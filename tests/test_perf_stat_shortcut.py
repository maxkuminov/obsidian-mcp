"""#282 — the scan's stat shortcut, its racy rule and its backstop.

An index pass used to read and SHA-256 every note on every tick to learn that
almost none had changed. It now skips a file's read when the file's current
`(size, mtime_ns, ctime_ns, inode)` equals the tuple recorded for the bytes
that produced its row — and the failure this product ranks highest, a
silently stale search result, is exactly what a wrong skip produces. So every
case here is about *when the file is read*:

* an unchanged stat is not read; a changed one is, and only its stat is
  written when the bytes turn out to be the same;
* every bypass reads — a NULL stat, a stale extraction marker, a re-derive, a
  full-hash (backstop) pass, and `INDEX_STAT_SHORTCUT=false`;
* a racy stat — fresh at read start, or from the future — is recorded NULL,
  measured against the instant taken *before* the read, so a slow read cannot
  launder it;
* a backstop pass that aborts, or commits with a skipped file, leaves the
  scope due;
* the one class the rule cannot see (same size, forced identical stat) is
  missed until the backstop, then picked up — accepted limitation L3, pinned.

Offline: an in-memory `notes_metadata` stands in for the database, because the
property is which files the scan opens and what it writes, not SQL. The
database half (C4/C5 under real interleavings, the A→B→A sweep) is
`tests/integration/test_perf_scan_pg.py`.
"""
from __future__ import annotations

import os
import time
from types import SimpleNamespace

import pytest
from sqlalchemy.sql.dml import Delete, Insert
from sqlalchemy.sql.elements import TextClause
from sqlalchemy.sql.selectable import Select

from src.services import indexer

SECOND = 1_000_000_000


# ══════════════════════════════════════════════════════════════════════════
# An in-memory notes_metadata, just enough of it for the index pass
# ══════════════════════════════════════════════════════════════════════════


class _Result:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.rowcount = len(self.rows)

    def fetchall(self):
        return self.rows

    def all(self):
        return self.rows

    def first(self):
        return self.rows[0] if self.rows else None

    def scalar(self):
        return None

    def scalar_one_or_none(self):
        return None


def _value(v):
    return getattr(v, "value", v)


STAT_KEYS = ("stat_size", "stat_mtime_ns", "stat_ctime_ns", "stat_ino")


class FakeIndexDB:
    """`notes_metadata` as a dict of path -> row, plus what the pass wrote.

    Understands the three statements the scan's behaviour turns on: the
    snapshot / locked re-read (a SELECT naming the stat columns), the upsert,
    and the unchanged-hash stat refresh (a conditional UPDATE by id, path and
    hash, applied only where all three still match). Everything else — the
    generation lock, links, keyword vectors — is patched out by `install`.
    """

    def __init__(self):
        self.rows: dict[str, dict] = {}
        self.next_id = 1
        self.upserts: list[dict] = []
        self.refreshes: list[dict] = []
        self.deleted: list = []
        self.commits = 0
        self.statements: list = []

    def add(self, path, content_hash, *, stat=None, xver=None):
        row = {
            "id": self.next_id,
            "file_path": path,
            "content_hash": content_hash,
            "extraction_version": (
                indexer.CURRENT_EXTRACTION_VERSION if xver is None else xver
            ),
            **dict(zip(STAT_KEYS, stat or (None,) * 4)),
        }
        self.next_id += 1
        self.rows[path] = row
        return row

    def stat_of(self, path):
        row = self.rows[path]
        values = tuple(row[k] for k in STAT_KEYS)
        return None if values[0] is None else values

    def session(self):
        return _FakeSession(self)


class _FakeSession:
    def __init__(self, db: FakeIndexDB):
        self.db = db

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def commit(self):
        self.db.commits += 1

    async def rollback(self):
        pass

    def begin_nested(self):
        class _Savepoint:
            async def __aenter__(self_inner):
                return self_inner

            async def __aexit__(self_inner, *exc):
                return False

        return _Savepoint()

    async def execute(self, stmt, params=None):
        db = self.db
        db.statements.append(stmt)
        if isinstance(stmt, Select) and "stat_size" in str(stmt):
            return _Result(SimpleNamespace(**row) for row in db.rows.values())
        if isinstance(stmt, Insert) and stmt.table.name == "notes_metadata":
            for group in stmt._multi_values:
                for raw in group:
                    values = {getattr(k, "name", str(k)): _value(v) for k, v in raw.items()}
                    db.upserts.append(values)
                    existing = db.rows.get(values["file_path"])
                    row = existing or {"id": db.next_id}
                    if existing is None:
                        db.next_id += 1
                    row.update({
                        "file_path": values["file_path"],
                        "content_hash": values["content_hash"],
                        "extraction_version": values["extraction_version"],
                        **{k: values[k] for k in STAT_KEYS},
                    })
                    db.rows[values["file_path"]] = row
            return _Result()
        if isinstance(stmt, TextClause) and "SET stat_size = :stat_size" in stmt.text:
            for p in params if isinstance(params, list) else [params]:
                db.refreshes.append(dict(p))
                row = db.rows.get(p["path"])
                if row and row["id"] == p["id"] and row["content_hash"] == p["hash"]:
                    row.update({k: p[k] for k in STAT_KEYS})
            return _Result()
        if isinstance(stmt, Delete):
            db.deleted.append(stmt)
        return _Result()


def install(monkeypatch, db: FakeIndexDB, vault):
    """Wire the real pass to `db` and `vault`; stub the unrelated stages."""
    monkeypatch.setattr(indexer, "async_session", db.session)
    monkeypatch.setattr(indexer, "_vault_root", lambda _uid: vault)
    monkeypatch.setattr(indexer, "_refuse_quarantined_pass", lambda *_a, **_k: None)

    async def _noop(*_a, **_k):
        return None

    monkeypatch.setattr(indexer, "acquire_generation_lock_unbounded", _noop)
    monkeypatch.setattr(indexer, "_assert_fts_generation_current", _noop)
    monkeypatch.setattr(indexer, "_update_links_for_changed", _noop)
    monkeypatch.setattr(indexer, "write_tsvector_bounded", _noop)
    monkeypatch.setattr(indexer.settings, "index_stat_shortcut", True, raising=False)


def settle_clock(monkeypatch, offset_s: float = 10.0):
    """Measure racy recency as if the pass ran `offset_s` from now, so files
    this test just wrote are old enough for their stat to be recorded."""
    monkeypatch.setattr(
        indexer, "_wall_clock_ns", lambda: time.time_ns() + int(offset_s * SECOND)
    )


def count_reads(monkeypatch):
    """Every file the scan (or a C4 re-read) actually opened, by name."""
    reads: list[str] = []
    real = indexer.read_note_at

    def counting(parent_fd, name):
        reads.append(name)
        return real(parent_fd, name)

    monkeypatch.setattr(indexer, "read_note_at", counting)
    return reads


def not_due(scope=None):
    """Mark the scope's backstop as just run, so the next pass may shortcut."""
    indexer._last_full_hash[scope] = time.monotonic()


def h(body: str) -> str:
    return indexer._content_hash(body)


@pytest.fixture
def vault(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    return root


# ══════════════════════════════════════════════════════════════════════════
# The shortcut itself
# ══════════════════════════════════════════════════════════════════════════


async def test_an_unchanged_stat_is_not_read(monkeypatch, vault):
    (vault / "Note.md").write_text("body\n", encoding="utf-8")
    db = FakeIndexDB()
    install(monkeypatch, db, vault)
    settle_clock(monkeypatch)

    await indexer.index_vault()  # first pass after start: full hash, records
    assert db.stat_of("Note.md") is not None
    assert not indexer._full_hash_due(None)

    reads = count_reads(monkeypatch)
    db.upserts.clear()
    await indexer.index_vault()

    assert reads == [], "the shortcut opened a file whose stat had not changed"
    assert db.upserts == [] and db.refreshes == []


async def test_a_touched_file_is_reread_and_only_its_stat_is_refreshed(
    monkeypatch, vault
):
    note = vault / "Note.md"
    note.write_text("body\n", encoding="utf-8")
    db = FakeIndexDB()
    install(monkeypatch, db, vault)
    settle_clock(monkeypatch)
    await indexer.index_vault()
    before = db.stat_of("Note.md")

    st = os.stat(note)
    os.utime(note, ns=(st.st_atime_ns, st.st_mtime_ns - 5 * SECOND))
    reads = count_reads(monkeypatch)
    db.upserts.clear()
    await indexer.index_vault()

    assert reads == ["Note.md"]
    assert db.upserts == [], "an unchanged hash must not be re-upserted"
    assert len(db.refreshes) == 1
    refresh = db.refreshes[0]
    assert (refresh["id"], refresh["path"], refresh["hash"]) == (
        db.rows["Note.md"]["id"], "Note.md", h("body\n"),
    )
    after = db.stat_of("Note.md")
    assert after is not None and after != before
    assert after == indexer._stat_tuple(os.stat(note))

    reads.clear()
    await indexer.index_vault()
    assert reads == [], "the refreshed stat must be trusted on the next pass"


async def test_a_changed_stat_with_new_bytes_is_upserted_with_its_stat(
    monkeypatch, vault
):
    note = vault / "Note.md"
    note.write_text("body\n", encoding="utf-8")
    db = FakeIndexDB()
    install(monkeypatch, db, vault)
    settle_clock(monkeypatch)
    await indexer.index_vault()

    note.write_text("a different, longer body\n", encoding="utf-8")
    reads = count_reads(monkeypatch)
    db.upserts.clear()
    await indexer.index_vault()

    assert reads == ["Note.md"]
    assert [u["content_hash"] for u in db.upserts] == [h("a different, longer body\n")]
    assert db.stat_of("Note.md") == indexer._stat_tuple(os.stat(note))


# ══════════════════════════════════════════════════════════════════════════
# Every bypass reads
# ══════════════════════════════════════════════════════════════════════════


async def _recorded_world(monkeypatch, vault, *, xver=None, null_stat=False):
    """One note whose row records its current stat (or NULL), non-backstop."""
    note = vault / "Note.md"
    note.write_text("body\n", encoding="utf-8")
    db = FakeIndexDB()
    install(monkeypatch, db, vault)
    settle_clock(monkeypatch)
    stat = None if null_stat else indexer._stat_tuple(os.stat(note))
    db.add("Note.md", h("body\n"), stat=stat, xver=xver)
    not_due(None)
    return db


async def test_the_control_case_is_not_read(monkeypatch, vault):
    """The bypass cases below are meaningful only if this one skips."""
    await _recorded_world(monkeypatch, vault)
    reads = count_reads(monkeypatch)
    await indexer.index_vault()
    assert reads == []


async def test_a_null_stat_is_read(monkeypatch, vault):
    db = await _recorded_world(monkeypatch, vault, null_stat=True)
    reads = count_reads(monkeypatch)
    await indexer.index_vault()
    assert reads == ["Note.md"]
    # And the unchanged-hash refresh records it for next time.
    assert db.stat_of("Note.md") is not None


async def test_a_stale_extraction_marker_is_read(monkeypatch, vault):
    db = await _recorded_world(
        monkeypatch, vault, xver=indexer.CURRENT_EXTRACTION_VERSION - 1
    )
    reads = count_reads(monkeypatch)
    await indexer.index_vault()
    assert reads == ["Note.md"]
    assert [u["file_path"] for u in db.upserts] == ["Note.md"]


async def test_a_full_hash_pass_is_read(monkeypatch, vault):
    await _recorded_world(monkeypatch, vault)
    reads = count_reads(monkeypatch)
    await indexer.index_vault(full_hash=True)
    assert reads == ["Note.md"]


async def test_a_disabled_shortcut_reads(monkeypatch, vault):
    await _recorded_world(monkeypatch, vault)
    monkeypatch.setattr(indexer.settings, "index_stat_shortcut", False, raising=False)
    reads = count_reads(monkeypatch)
    await indexer.index_vault()
    assert reads == ["Note.md"]


async def test_a_re_derive_reads(monkeypatch, vault):
    await _recorded_world(monkeypatch, vault)
    not_due(7)

    async def re_derive(user_id, _vault, _root_fd, _suffix):
        return True, None

    monkeypatch.setattr(indexer, "_reconcile_provenance", re_derive)
    reads = count_reads(monkeypatch)
    await indexer.index_vault(user_id=7)
    assert reads == ["Note.md"]


async def test_the_first_pass_for_a_scope_is_a_full_hash_pass(monkeypatch, vault):
    await _recorded_world(monkeypatch, vault)
    indexer._last_full_hash.clear()  # a process start
    reads = count_reads(monkeypatch)
    await indexer.index_vault()
    assert reads == ["Note.md"]


async def test_the_interval_makes_the_scope_due_again(monkeypatch, vault):
    await _recorded_world(monkeypatch, vault)
    monkeypatch.setattr(
        indexer.settings, "index_full_hash_interval_hours", 1, raising=False
    )
    indexer._last_full_hash[None] = time.monotonic() - 3601
    reads = count_reads(monkeypatch)
    await indexer.index_vault()
    assert reads == ["Note.md"]


# ══════════════════════════════════════════════════════════════════════════
# The racy rule
# ══════════════════════════════════════════════════════════════════════════


def _stat(mtime_ns, ctime_ns, *, size=5, ino=42):
    return SimpleNamespace(
        st_size=size, st_mtime_ns=mtime_ns, st_ctime_ns=ctime_ns, st_ino=ino,
        st_mtime=mtime_ns / SECOND,
    )


@pytest.mark.parametrize(
    "label,mtime,ctime,recorded",
    [
        ("old", -10, -10, True),
        ("mtime_within_window", -1, -10, False),
        ("ctime_within_window", -10, -1, False),
        ("exactly_at_the_window_edge", -2, -10, False),
        ("future_mtime", +1, -10, False),
        ("future_ctime", -10, +1, False),
    ],
)
def test_the_racy_rule(label, mtime, ctime, recorded):
    t_start = 1_000 * SECOND
    st = _stat(t_start + mtime * SECOND, t_start + ctime * SECOND)
    result = indexer._recordable_stat(st, t_start)
    assert (result is not None) is recorded, label


def test_a_high_inode_is_stored_signed():
    st = _stat(0, 0, ino=2**64 - 1)
    assert indexer._stat_tuple(st)[3] == -1
    assert indexer._stat_tuple(_stat(0, 0, ino=2**63 - 1))[3] == 2**63 - 1


async def test_a_freshly_written_file_records_a_null_stat(monkeypatch, vault):
    """No clock offset: the file was written moments ago, which is racy."""
    (vault / "Fresh.md").write_text("body\n", encoding="utf-8")
    db = FakeIndexDB()
    install(monkeypatch, db, vault)
    await indexer.index_vault()
    assert [u["file_path"] for u in db.upserts] == ["Fresh.md"]
    assert all(db.upserts[0][k] is None for k in STAT_KEYS)


async def test_a_slow_read_does_not_launder_a_same_tick_rewrite(monkeypatch, vault):
    """Codex spec review r1, MAJOR, as a test.

    The file's timestamps are frozen at one tick, fresh at read start. Mid-read
    a writer rewrites already-read bytes at the same size — same tick, so the
    stat does not move — and the read then takes longer than the racy window.
    Measured at hash completion, the timestamp would look old and be recorded,
    and the next pass would skip the rewritten file for ever. Measured at read
    start, it is recorded NULL and the next pass reads the new bytes.
    """
    note = vault / "Note.md"
    note.write_text("AAAA\n", encoding="utf-8")
    db = FakeIndexDB()
    install(monkeypatch, db, vault)
    not_due(None)  # a non-backstop pass, so only the racy rule stands guard

    clock = {"now": 5_000 * SECOND}
    monkeypatch.setattr(indexer, "_wall_clock_ns", lambda: clock["now"])
    frozen = _stat(clock["now"] - 1 * SECOND, clock["now"] - 1 * SECOND)
    real_read = indexer.read_note_at

    def slow_read_with_a_same_tick_rewrite(parent_fd, name):
        raw, _real_stat = real_read(parent_fd, name)
        if raw == "AAAA\n":
            note.write_text("BBBB\n", encoding="utf-8")  # same size, same tick
            clock["now"] += 3 * SECOND  # the read outlasts the window
        return raw, frozen

    monkeypatch.setattr(indexer, "read_note_at", slow_read_with_a_same_tick_rewrite)
    monkeypatch.setattr(indexer, "_stat_follow", lambda _fd, _name: frozen)

    await indexer.index_vault()
    assert db.rows["Note.md"]["content_hash"] == h("AAAA\n")
    assert db.stat_of("Note.md") is None, "a fresh-at-read-start stat was trusted"

    await indexer.index_vault()
    assert db.rows["Note.md"]["content_hash"] == h("BBBB\n"), (
        "the next pass skipped the rewritten bytes"
    )


# ══════════════════════════════════════════════════════════════════════════
# The backstop only counts when it verified everything
# ══════════════════════════════════════════════════════════════════════════


async def test_a_backstop_that_aborts_leaves_the_scope_due(monkeypatch, vault):
    db = await _recorded_world(monkeypatch, vault)
    indexer._last_full_hash.clear()  # due
    (vault / "New.md").write_text("new\n", encoding="utf-8")

    async def boom(*_a, **_k):
        raise RuntimeError("link rebuild failed")

    monkeypatch.setattr(indexer, "_update_links_for_changed", boom)
    with pytest.raises(RuntimeError):
        await indexer.index_vault()
    assert indexer._full_hash_due(None), "an aborted backstop advanced the clock"

    async def _noop(*_a, **_k):
        return None

    monkeypatch.setattr(indexer, "_update_links_for_changed", _noop)
    reads = count_reads(monkeypatch)
    await indexer.index_vault()
    assert sorted(reads) == ["New.md", "Note.md"], (
        "the pass after an aborted backstop must again read every file"
    )
    assert not indexer._full_hash_due(None)
    assert db.commits >= 1


async def test_a_backstop_with_an_unreadable_file_leaves_the_scope_due(
    monkeypatch, vault
):
    """D12 (Codex r2): the scan catches a read failure and still commits, so
    "committed" alone would let the skipped file hide for another interval."""
    await _recorded_world(monkeypatch, vault)
    (vault / "Bad.md").write_bytes(b"\xff\xfe not utf-8")
    indexer._last_full_hash.clear()

    await indexer.index_vault()
    assert indexer._full_hash_due(None)

    reads = count_reads(monkeypatch)
    await indexer.index_vault()
    assert "Note.md" in reads, "the next pass was not a full-hash pass"


async def test_a_clean_backstop_advances_the_clock(monkeypatch, vault):
    await _recorded_world(monkeypatch, vault)
    indexer._last_full_hash.clear()
    await indexer.index_vault()
    assert not indexer._full_hash_due(None)


# ══════════════════════════════════════════════════════════════════════════
# What the stat can and cannot see
# ══════════════════════════════════════════════════════════════════════════


async def test_a_retargeted_symlink_is_reread(monkeypatch, vault):
    """The comparison stat follows the leaf, as the read does, so repointing
    `Link.md` presents the new target's inode even at identical content."""
    (vault / "one.dat").write_text("same body\n", encoding="utf-8")
    (vault / "two.dat").write_text("same body\n", encoding="utf-8")
    link = vault / "Link.md"
    link.symlink_to("one.dat")
    db = FakeIndexDB()
    install(monkeypatch, db, vault)
    settle_clock(monkeypatch)
    await indexer.index_vault()
    assert db.stat_of("Link.md")[3] == os.stat(vault / "one.dat").st_ino

    link.unlink()
    link.symlink_to("two.dat")
    reads = count_reads(monkeypatch)
    await indexer.index_vault()
    assert reads == ["Link.md"]
    assert db.stat_of("Link.md")[3] == os.stat(vault / "two.dat").st_ino


async def test_a_same_size_rewrite_behind_an_identical_stat_waits_for_the_backstop(
    monkeypatch, vault
):
    """Accepted limitation L3, pinned rather than hidden: an edit that changes
    none of the four fields is not detected until the next full-hash pass."""
    note = vault / "Note.md"
    note.write_text("AAAA\n", encoding="utf-8")
    db = FakeIndexDB()
    install(monkeypatch, db, vault)
    settle_clock(monkeypatch)
    await indexer.index_vault()
    old = os.stat(note)

    note.write_text("BBBB\n", encoding="utf-8")
    monkeypatch.setattr(indexer, "_stat_follow", lambda _fd, _name: old)
    await indexer.index_vault()
    assert db.rows["Note.md"]["content_hash"] == h("AAAA\n"), (
        "control: a forced identical stat is supposed to hide the edit"
    )

    indexer._last_full_hash[None] -= 25 * 3600  # the interval elapses
    await indexer.index_vault()
    assert db.rows["Note.md"]["content_hash"] == h("BBBB\n")


# ══════════════════════════════════════════════════════════════════════════
# The scan function alone
# ══════════════════════════════════════════════════════════════════════════


def test_the_scan_retains_a_body_only_where_the_pass_needs_it(vault):
    import threading

    for name, body in (("Same.md", "same\n"), ("Changed.md", "new\n"), ("New.md", "n\n")):
        (vault / name).write_text(body, encoding="utf-8")
    cur = indexer.CURRENT_EXTRACTION_VERSION
    snapshot = {
        "Same.md": indexer.SnapshotRow(h("same\n"), cur, None),
        "Changed.md": indexer.SnapshotRow(h("old\n"), cur, None),
    }
    with indexer.pinned_root(vault) as fd:
        result = indexer._scan_vault(
            fd, snapshot, force_read=False, re_derive=False, stop=threading.Event()
        )
    assert result.seen == {"Same.md", "Changed.md", "New.md"}
    assert result.files["Same.md"].raw is None
    assert result.files["Changed.md"].raw == "new\n"
    assert result.files["New.md"].raw == "n\n"

    with indexer.pinned_root(vault) as fd:
        result = indexer._scan_vault(
            fd, snapshot, force_read=True, re_derive=True, stop=threading.Event()
        )
    assert result.files["Same.md"].raw == "same\n", "a re-derive needs every body"
