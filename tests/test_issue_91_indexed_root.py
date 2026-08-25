"""The index-provenance record and the reconciling pass (issue #91).

`notes_metadata.file_path` is vault-relative and nothing recorded which vault
assignment a user's rows were built under, so after an administrator repointed
a user at another vault the database-backed tools — `semantic_search`,
`keyword_search`, `list_notes`, `get_recent` and every graph tool — went on
answering from the *previous* vault. Migration 016 adds the record; this module
covers what the pass does with it.

**Read the classification as a preference order, because that is what the tests
assert.** Ambiguity never resolves toward *keeping*, because silently wrong
search results are the failure this product ranks highest — an agent acts on
them without a human ever seeing the query. Ambiguity never resolves toward
*discarding* either, because a discard costs a full re-embed of the vault.
Everything between goes to a branch that asserts nothing and destroys nothing,
and only **unanimous** disagreement destroys.

Two of the cases here are review blockers with a shared shape, and both use
**real** filesystem objects rather than mocks, because the defect is in what
the kernel can hand back: an over-long real path and a real path with a
non-UTF-8 component. In each, a value the pass observed and could not store
would raise *inside the discard transaction*, roll the delete back with it, and
leave the former vault's index queryable on every subsequent pass — #91's own
symptom, produced by a column definition.

Offline: a recording fake session, a real `tmp_path` vault, a real
`name_to_handle_at`.
"""

import errno
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.sql.dml import Delete, Update
from sqlalchemy.sql.elements import TextClause
from sqlalchemy.sql.selectable import Select

from src.services import embeddings, indexer, transfer


# ── the recording fake session ─────────────────────────────────────────────
#
# `index_vault` issues a handful of statement shapes and reads three of them.
# Dispatching on the shape rather than on call order keeps the tests readable
# and stops a reordering inside the pass from silently feeding a select the
# wrong rows.


class _LockNotAvailable(Exception):
    """What `SELECT … FOR UPDATE NOWAIT` raises on a contended row.

    Shaped like the real thing: SQLAlchemy's wrapper carries `.orig`, whose
    `__cause__` is asyncpg's own error, and only the innermost one knows the
    SQLSTATE. `indexer._is_lock_not_available` has to walk both layers.
    """

    def __init__(self) -> None:
        super().__init__("could not obtain lock on row in relation \"users\"")
        inner = Exception("lock_not_available")
        inner.sqlstate = indexer.LOCK_NOT_AVAILABLE
        dialect_level = Exception("(asyncpg.exceptions.LockNotAvailableError)")
        dialect_level.__cause__ = inner
        self.orig = dialect_level


class _Result:
    def __init__(self, rows=(), rowcount=0):
        self.rows = list(rows)
        self.rowcount = rowcount

    def fetchall(self):
        return self.rows

    def all(self):
        return self.rows

    def first(self):
        return self.rows[0] if self.rows else None

    def scalar(self):
        return self.rows[0] if self.rows else None


class FakeSession:
    """One session object shared by every `async_session()` in a pass.

    `provenance` is the recorded triple the classification reads.
    `existing` is `{rel_path: content_hash}` — the user's `notes_metadata`.
    `note_ids` is `{rel_path: id}` for the link rebuild's vault index.
    """

    def __init__(
        self, *, provenance=(None, None, None), existing=None, note_ids=None,
        link_count=0, assignment=None, is_active=True,
    ):
        self.provenance = provenance
        # What the locked, freshly read `users` row says (#91, adversarial
        # round 1 BLOCKER). `install()` fills it in from the vault the pass is
        # given; a test that wants the reviewer's interleaving sets it to
        # something else, or flips it part way through with `flip_assignment`.
        self.assignment = assignment
        self.is_active = is_active
        self.user_row_missing = False
        # Set by a test to make a `FOR UPDATE NOWAIT` on `users` fail the way
        # PostgreSQL does when somebody else already holds the row.
        self.users_row_locked = False
        # Every locked re-read of the `users` row, in order.
        self.assignment_reads = 0
        # What `link_backfill_pass`'s completion probe sees. 0 means "no pass
        # has completed for this scope yet", which is when it runs.
        self.link_count = link_count
        self.existing = dict(existing or {})
        self.note_ids = dict(note_ids or {})
        self.statements: list = []
        # A short label per statement, per commit and per vault-file read, in
        # order. Ordering is half of what this record has to get right: the
        # discard and its stamp must be one transaction, and both must land
        # before the first file under the new root is opened.
        self.timeline: list[str] = []
        self.commits = 0
        # Every `notes_metadata` DELETE this pass issued, so a test can prove
        # the discard happened exactly once.
        self.metadata_deletes: list = []
        # Every `users` UPDATE, as the values mapping — the provenance stamps.
        self.stamps: list[dict] = []
        # Set by a test to make a specific statement raise.
        self.fail_on = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def close(self):
        pass

    async def rollback(self):
        pass

    def begin_nested(self):
        """The savepoint the tail stamp takes its `NOWAIT` lock inside.

        Records entry and exit so a test can prove the stamp really is scoped
        to one, and re-raises whatever the body raised — which is what makes
        "roll back only the savepoint, keep the repairs" observable here.
        """
        session = self

        class _Savepoint:
            async def __aenter__(self):
                session.timeline.append("savepoint")
                return self

            async def __aexit__(self, exc_type, exc, tb):
                session.timeline.append(
                    "savepoint:rollback" if exc_type is not None else "savepoint:release"
                )
                return False

        return _Savepoint()

    async def commit(self):
        self.commits += 1
        self.timeline.append("commit")

    async def execute(self, stmt, params=None):
        self.statements.append(stmt)
        if self.fail_on is not None and self.fail_on(stmt):
            self.timeline.append("raise")
            raise RuntimeError("injected failure")

        if isinstance(stmt, Update) and _table_of(stmt) == "users":
            values = dict(stmt._values or {})
            self.stamps.append(
                {k.name if hasattr(k, "name") else str(k): _literal(v)
                 for k, v in values.items()}
            )
            self.timeline.append("stamp")
            # Exactly the one row the caller locked. A stamp that matched no
            # row aborts the transaction it is in.
            return _Result(rowcount=0 if self.user_row_missing else 1)

        if isinstance(stmt, Delete):
            table = _table_of(stmt)
            if table == "notes_metadata":
                self.metadata_deletes.append(stmt)
                # The whole-index discard names no `file_path`; the ordinary
                # prune names the paths it removes. Telling them apart is what
                # lets a test assert "re-derive, and *not* a discard".
                whole = "file_path" not in str(stmt)
                self.timeline.append(
                    "discard" if whole else "prune"
                )
                return _Result(rowcount=len(self.existing) if whole else 0)
            self.timeline.append(f"delete:{table}")
            return _Result()

        if isinstance(stmt, Select):
            rendered = str(stmt)
            if "indexed_vault_assignment" in rendered:
                return _Result([self.provenance])
            # The locked re-read that binds a delete or a stamp to the
            # assignment that produced it.
            if "users.vault_path" in rendered and "FOR UPDATE" in rendered:
                self.assignment_reads += 1
                # `NOWAIT` is PostgreSQL-specific and the default dialect does
                # not render it, so the construct is read rather than the SQL
                # string — which is the stronger assertion anyway.
                nowait = bool(
                    getattr(getattr(stmt, "_for_update_arg", None), "nowait", False)
                )
                self.timeline.append("lock:users:nowait" if nowait else "lock:users")
                if nowait and self.users_row_locked:
                    raise _LockNotAvailable()
                if self.user_row_missing:
                    return _Result([])
                return _Result([
                    SimpleNamespace(
                        vault_path=self.assignment, is_active=self.is_active
                    )
                ])
            # `link_backfill_pass`'s "has this user any links yet" probe. Its
            # join condition mentions `notes_metadata.id`, so it has to be
            # recognised before the vault-index branch below.
            if rendered.startswith("SELECT count("):
                return _Result([self.link_count])
            if "content_hash" in rendered:
                return _Result([
                    SimpleNamespace(file_path=p, content_hash=h)
                    for p, h in self.existing.items()
                ])
            if "notes_metadata.id" in rendered:
                return _Result([
                    SimpleNamespace(file_path=p, id=i)
                    for p, i in self.note_ids.items()
                ])
        return _Result()

    # ── read-back helpers ──────────────────────────────────────────────
    def _inserted(self, table: str) -> list[dict]:
        """Every row handed to `insert(...).values([...])` for `table`."""
        rows: list[dict] = []
        for stmt in self.statements:
            if not getattr(stmt, "is_insert", False):
                continue
            if _table_of(stmt) != table:
                continue
            for group in stmt._multi_values:
                rows.extend(
                    {getattr(k, "name", str(k)): _literal(v) for k, v in row.items()}
                    for row in group
                )
        return rows

    def link_inserts(self) -> list[dict]:
        return self._inserted("note_links")

    def metadata_upserts(self) -> list[dict]:
        return self._inserted("notes_metadata")

    def note_link_deletes(self) -> list:
        return [
            s for s in self.statements
            if isinstance(s, Delete) and _table_of(s) == "note_links"
        ]


def _table_of(stmt) -> str:
    """The SQL table a statement targets.

    Not `entity_description["name"]`, which is the ORM *class* name
    (`NoteMetadata`) rather than the table (`notes_metadata`).
    """
    return stmt.table.name


def _literal(value):
    """Unwrap a bound literal so a stamp's values compare as plain Python."""
    return getattr(value, "value", value)


def install(monkeypatch, session, vault):
    monkeypatch.setattr(indexer, "async_session", lambda: session)
    monkeypatch.setattr(indexer, "_vault_root", lambda _uid: vault)
    # By default the database agrees with the root the pass was handed — the
    # ordinary case. A test that wants the reviewer's interleaving sets
    # `session.assignment` itself, before or during the pass.
    if session.assignment is None:
        session.assignment = str(vault)


def facts_for(vault: Path) -> indexer.RootFacts:
    """The three facts as the pass would observe them for `vault`."""
    with indexer.pinned_root(vault) as fd:
        observed = indexer.observe_root_facts(vault, fd)
    assert observed is not None
    return observed


def recorded_from(facts: indexer.RootFacts):
    """The recorded triple a stamp of `facts` would have written."""
    return (facts.assignment, facts.realpath_hex, facts.handle)


def make_vault(tmp_path: Path, name: str, notes: dict[str, str]) -> Path:
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    for rel, body in notes.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    return root


# ══════════════════════════════════════════════════════════════════════════
# The classification table — total over every combination of inputs
# ══════════════════════════════════════════════════════════════════════════


def test_the_assignment_and_the_realpath_both_agree_so_nothing_is_done(tmp_path):
    vault = make_vault(tmp_path, "A", {"a.md": "a"})
    facts = facts_for(vault)

    verdict = indexer.classify_provenance(*recorded_from(facts), facts)

    assert verdict.verdict == indexer.PROVENANCE_KEEP


def test_unanimous_disagreement_is_the_only_thing_that_discards(tmp_path):
    a = make_vault(tmp_path, "A", {"a.md": "a"})
    b = make_vault(tmp_path, "B", {"b.md": "b"})

    verdict = indexer.classify_provenance(*recorded_from(facts_for(a)), facts_for(b))

    assert verdict.verdict == indexer.PROVENANCE_DISCARD


def test_a_cosmetic_respelling_is_not_a_reassignment(tmp_path):
    """A trailing separator, a doubled separator and a `.` component all
    normalise away through the one shared normaliser."""
    vault = make_vault(tmp_path, "A", {"a.md": "a"})
    recorded = recorded_from(facts_for(vault))

    for spelling in (
        Path(str(vault) + "/"),
        Path(str(vault).replace(f"{os.sep}A", f"{os.sep}{os.sep}A")),
        Path(str(tmp_path)) / "." / "A",
    ):
        facts = facts_for(spelling)
        verdict = indexer.classify_provenance(*recorded, facts)
        assert verdict.verdict == indexer.PROVENANCE_KEEP, (
            f"{spelling} was not recognised as the same assignment"
        )


def test_two_aliases_of_one_directory_re_derive_rather_than_discard(tmp_path):
    """An operator spelling the root differently must not cost a re-embed.

    The strings differ and the real paths agree, so exactly one fact disagrees
    — which is the cheap, safe branch by construction.
    """
    real = make_vault(tmp_path, "real", {"a.md": "a"})
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)

    recorded = recorded_from(facts_for(real))
    verdict = indexer.classify_provenance(*recorded, facts_for(alias))

    assert verdict.verdict == indexer.PROVENANCE_REDERIVE
    assert verdict.verdict != indexer.PROVENANCE_DISCARD


def test_a_retargeted_symlink_under_an_unchanged_assignment_re_derives(tmp_path):
    """Assignment equal, real path different: re-derive, never discard, never
    keep."""
    a = make_vault(tmp_path, "A", {"a.md": "a"})
    b = make_vault(tmp_path, "B", {"b.md": "b"})
    link = tmp_path / "current"
    link.symlink_to(a, target_is_directory=True)
    recorded = recorded_from(facts_for(link))

    link.unlink()
    link.symlink_to(b, target_is_directory=True)

    verdict = indexer.classify_provenance(*recorded, facts_for(link))

    assert verdict.verdict == indexer.PROVENANCE_REDERIVE


@pytest.mark.parametrize(
    "assignment,realpath",
    [("/vaults/a", None), (None, "6162"), (None, None)],
)
def test_a_half_set_record_is_no_record(assignment, realpath, tmp_path):
    """Both facts are always observable for a root the pass could pin, so a
    half-set record is drift rather than a state this code writes — and the
    safe reading of drift is that nothing is known, not that the half that is
    set may be trusted."""
    vault = make_vault(tmp_path, "A", {"a.md": "a"})

    verdict = indexer.classify_provenance(assignment, realpath, None, facts_for(vault))

    assert verdict.verdict == indexer.PROVENANCE_REDERIVE


def test_an_unpinnable_root_is_indeterminate(tmp_path):
    """No delete, no stamp: an index cannot be re-derived from a directory that
    cannot be read, and destroying one because a mount was briefly unavailable
    buys nothing and costs the full re-embed."""
    verdict = indexer.classify_provenance("/vaults/a", "6162", None, None)

    assert verdict.verdict == indexer.PROVENANCE_INDETERMINATE


def test_a_root_whose_realpath_stops_naming_the_pinned_inode_is_indeterminate(tmp_path):
    """Device and inode numbers are used *only* here, within one moment, to
    establish that the real path being recorded describes the inode being
    pinned. They are never stored and never compared across passes."""
    a = make_vault(tmp_path, "A", {"a.md": "a"})
    b = make_vault(tmp_path, "B", {"b.md": "b"})
    link = tmp_path / "current"
    link.symlink_to(a, target_is_directory=True)

    with indexer.pinned_root(link) as fd:
        # The pathname now names a different inode than the descriptor.
        link.unlink()
        link.symlink_to(b, target_is_directory=True)
        observed = indexer.observe_root_facts(link, fd)

    assert observed is None
    assert (
        indexer.classify_provenance("x", "y", None, observed).verdict
        == indexer.PROVENANCE_INDETERMINATE
    )


# ══════════════════════════════════════════════════════════════════════════
# The handle: hardening in the refusing direction only
# ══════════════════════════════════════════════════════════════════════════


def test_a_contradicting_handle_downgrades_a_keep_to_a_re_derive(tmp_path):
    """A real filesystem operation, not a mock.

    On ext4 and xfs a directory's handle is its inode number plus the inode's
    **generation counter**, which the kernel bumps precisely so a reused inode
    is not mistaken for the old one. So `rmdir` + `mkdir` at the same pathname
    leaves the assignment and the real path identical while the handle moves —
    and the pass re-derives cheaply rather than keeping. It does **not**
    discard: a replacement at the same pathname under an unchanged assignment
    is as likely to be a restore as anything else.
    """
    vault = tmp_path / "A"
    vault.mkdir()
    before = facts_for(vault)
    if before.handle is None:  # pragma: no cover - filesystem dependent
        pytest.skip("this filesystem cannot produce a file handle")

    for _ in range(4000):
        vault.rmdir()
        vault.mkdir()
        after = facts_for(vault)
        if after.handle != before.handle:
            break
    else:  # pragma: no cover - would mean the handle never changed
        pytest.skip("the handle never changed across a rmdir/mkdir cycle")

    assert after.assignment == before.assignment
    assert after.realpath_hex == before.realpath_hex

    verdict = indexer.classify_provenance(*recorded_from(before), after)

    assert verdict.verdict == indexer.PROVENANCE_REDERIVE
    assert verdict.verdict != indexer.PROVENANCE_DISCARD


def test_a_matching_handle_never_produces_a_keep(tmp_path):
    """The other direction, explicitly. A matching handle grants nothing and
    never upgrades a verdict."""
    a = make_vault(tmp_path, "A", {"a.md": "a"})
    b = make_vault(tmp_path, "B", {"b.md": "b"})
    facts_a = facts_for(a)
    facts_b = facts_for(b)
    shared = facts_a.handle or "1:deadbeef"

    # Both facts disagree, handle agrees → still a discard.
    unanimous = indexer.classify_provenance(
        facts_a.assignment,
        facts_a.realpath_hex,
        shared,
        indexer.RootFacts(
            facts_b.assignment, facts_b.realpath, facts_b.realpath_hex, shared
        ),
    )
    assert unanimous.verdict == indexer.PROVENANCE_DISCARD

    # One fact disagrees, handle agrees → still a re-derive, never a keep.
    partial = indexer.classify_provenance(
        facts_a.assignment,
        facts_b.realpath_hex,
        shared,
        indexer.RootFacts(
            facts_a.assignment, facts_a.realpath, facts_a.realpath_hex, shared
        ),
    )
    assert partial.verdict == indexer.PROVENANCE_REDERIVE


def test_an_unavailable_handle_removes_a_refusal_and_nothing_else(tmp_path):
    """A NULL handle means "no hardening signal", never "provenance unknown".

    Where no handle is available on either side the pass reaches exactly the
    verdict the other two facts imply — no degraded mode, no extra re-derive,
    and nothing said to the operator.
    """
    a = make_vault(tmp_path, "A", {"a.md": "a"})
    b = make_vault(tmp_path, "B", {"b.md": "b"})
    facts_a = facts_for(a)
    facts_b = facts_for(b)
    handleless_a = indexer.RootFacts(
        facts_a.assignment, facts_a.realpath, facts_a.realpath_hex, None
    )
    handleless_b = indexer.RootFacts(
        facts_b.assignment, facts_b.realpath, facts_b.realpath_hex, None
    )

    # The keep case.
    assert (
        indexer.classify_provenance(
            facts_a.assignment, facts_a.realpath_hex, None, handleless_a
        ).verdict
        == indexer.PROVENANCE_KEEP
    )
    # The discard case.
    assert (
        indexer.classify_provenance(
            facts_a.assignment, facts_a.realpath_hex, None, handleless_b
        ).verdict
        == indexer.PROVENANCE_DISCARD
    )
    # And a recorded handle with none readable now is still no mismatch.
    assert (
        indexer.classify_provenance(
            facts_a.assignment, facts_a.realpath_hex, "1:deadbeef", handleless_a
        ).verdict
        == indexer.PROVENANCE_KEEP
    )


def test_read_dir_handle_answers_none_rather_than_raising_on_an_unsupported_fs():
    """`EOPNOTSUPP` on procfs is the ordinary path, not an error path."""
    fd = os.open("/proc", os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert indexer.read_dir_handle(fd) is None
    finally:
        os.close(fd)


# ══════════════════════════════════════════════════════════════════════════
# The shared normaliser
# ══════════════════════════════════════════════════════════════════════════


def test_the_assignment_fact_is_produced_by_transfer_canonical_vault_root(
    monkeypatch, tmp_path
):
    """One normaliser, called rather than re-implemented.

    The index's notion of "the same assignment" and the write path's notion of
    it must not be able to drift apart, so the index calls the *same* function
    the pre-publish confirmation compares with. Patching it moves both; a
    second `str(Path(path))` in the indexer would leave this test asserting a
    value the write path no longer agrees with.
    """
    vault = make_vault(tmp_path, "A", {"a.md": "a"})
    monkeypatch.setattr(
        indexer, "canonical_vault_root", lambda path: f"canonical::{path}"
    )

    facts = facts_for(vault)

    assert facts.assignment == f"canonical::{vault}"
    # The realpath is a *separate* recorded fact rather than a second
    # normalisation of the assignment, and does not enter the comparison the
    # write path performs.
    assert facts.realpath_hex == os.fsencode(os.path.realpath(vault)).hex()
    assert indexer.canonical_vault_root is not transfer.canonical_vault_root


def test_the_indexer_imports_the_normaliser_rather_than_copying_it():
    """The unpatched identity, so the import cannot quietly become a copy."""
    assert indexer.canonical_vault_root is transfer.canonical_vault_root


# ══════════════════════════════════════════════════════════════════════════
# The encoding — total over the fact by construction, not by a bound
# ══════════════════════════════════════════════════════════════════════════


def test_the_realpath_encoding_round_trips_a_non_utf8_pathname(tmp_path):
    try:
        raw = os.path.join(os.fsencode(str(tmp_path)), b"vault-\xff-name")
        os.mkdir(raw)
    except (OSError, ValueError) as e:  # pragma: no cover - filesystem dependent
        pytest.skip(f"this filesystem refuses a non-UTF-8 directory name: {e}")

    realpath = os.path.realpath(os.fsdecode(raw))
    assert any("\udc80" <= ch <= "\udcff" for ch in realpath), realpath

    encoded = indexer.encode_realpath(realpath)

    assert encoded == encoded.lower()
    assert indexer.decode_realpath(encoded) == realpath
    # The raw string is exactly what a UTF-8 driver cannot take.
    with pytest.raises(UnicodeEncodeError):
        realpath.encode("utf-8")


# ══════════════════════════════════════════════════════════════════════════
# Driving the pass: discard
# ══════════════════════════════════════════════════════════════════════════


def track_reads(monkeypatch, session):
    """Record every vault-file read on the session's timeline."""
    real = indexer.read_note_at

    def wrapped(parent_fd, name):
        session.timeline.append(f"read:{name}")
        return real(parent_fd, name)

    monkeypatch.setattr(indexer, "read_note_at", wrapped)


def hash_of(body: str) -> str:
    return indexer._content_hash(body)


@pytest.mark.asyncio
async def test_reassignment_to_a_different_vault_discards(monkeypatch, tmp_path):
    """The transition #91 is about: `/old -> unassigned -> /new`, two Saves.

    The rows from the previous directory go before the new root is scanned, so
    the metadata-only tools stop answering from a vault the caller no longer
    has.
    """
    old = make_vault(tmp_path, "old", {"Note.md": "old body\n"})
    new = make_vault(tmp_path, "new", {"Fresh.md": "new body\n"})
    session = FakeSession(
        provenance=recorded_from(facts_for(old)),
        existing={"Note.md": hash_of("old body\n")},
        note_ids={"Fresh.md": 10},
    )
    install(monkeypatch, session, new)
    track_reads(monkeypatch, session)

    await indexer.index_vault(user_id=7)

    assert session.timeline.count("discard") == 1
    # `notes_metadata` scoped to this user; embeddings cascade and links
    # cascade on `source_note_id` / null out on `target_note_id`.
    assert "notes_metadata.user_id = :user_id_1" in str(session.metadata_deletes[0])
    # One transaction, and it commits before the first file is opened.
    discard_at = session.timeline.index("discard")
    stamp_at = session.timeline.index("stamp")
    commit_at = session.timeline.index("commit")
    assert discard_at < stamp_at < commit_at, session.timeline
    assert "commit" not in session.timeline[discard_at:stamp_at], session.timeline
    first_read = next(i for i, e in enumerate(session.timeline) if e.startswith("read:"))
    assert commit_at < first_read, session.timeline
    # And the recorded provenance is the new root's, whole.
    assert session.stamps[0] == {
        "indexed_vault_assignment": indexer.canonical_vault_root(new),
        "indexed_vault_realpath": indexer.encode_realpath(os.path.realpath(new)),
        "indexed_vault_handle": facts_for(new).handle,
    }


@pytest.mark.asyncio
async def test_a_discard_is_bound_to_the_assignment_that_produced_it(
    monkeypatch, tmp_path
):
    """Adversarial round 1's BLOCKER, exactly as it was described.

    User 7 is assigned to A and has a complete A index stamped A. The
    administrator assigns B; the pass caches and pins B and classifies
    A-versus-B as a discard. Before the discard transaction starts, the
    administrator corrects the assignment **back to A**. The old code deleted
    every A row and stamped provenance B, destroying a valid index and forcing
    a full re-embed of B — which the next pass would then discard again.

    The delete is bound to the assignment that produced the verdict: the
    transaction locks the `users` row, re-reads it, and finds A. Nothing is
    deleted, nothing is stamped, and the pass aborts so the next one
    reclassifies against the row as it now stands.
    """
    old = make_vault(tmp_path, "old", {"Note.md": "old body\n"})
    new = make_vault(tmp_path, "new", {"Fresh.md": "new body\n"})
    session = FakeSession(
        provenance=recorded_from(facts_for(old)),
        existing={"Note.md": hash_of("old body\n")},
        note_ids={"Fresh.md": 10},
        # The database says A again by the time the discard transaction runs.
        assignment=str(old),
    )
    install(monkeypatch, session, new)
    track_reads(monkeypatch, session)

    with pytest.raises(RuntimeError) as excinfo:
        await indexer.index_vault(user_id=7)

    assert "discard aborted" in str(excinfo.value)
    assert str(old) in str(excinfo.value)
    assert session.assignment_reads == 1
    assert session.metadata_deletes == [], "a valid index was destroyed"
    assert session.stamps == [], "provenance was recorded for a root nobody named"
    assert not any(e.startswith("read:") for e in session.timeline), session.timeline


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "condition", ["unassigned", "deactivated", "deleted"]
)
async def test_a_discard_aborts_when_the_locked_row_no_longer_describes_the_pass(
    monkeypatch, tmp_path, condition
):
    """The other three states the locked re-read can find. None of them is the
    state the classification described, so none licenses the delete."""
    old = make_vault(tmp_path, "old", {"Note.md": "old body\n"})
    new = make_vault(tmp_path, "new", {"Fresh.md": "new body\n"})
    session = FakeSession(
        provenance=recorded_from(facts_for(old)),
        existing={"Note.md": hash_of("old body\n")},
        note_ids={"Fresh.md": 10},
        assignment=str(new),
    )
    install(monkeypatch, session, new)
    if condition == "unassigned":
        session.assignment = None
    elif condition == "deactivated":
        session.is_active = False
    else:
        session.user_row_missing = True

    with pytest.raises(RuntimeError) as excinfo:
        await indexer.index_vault(user_id=7)

    assert "discard aborted" in str(excinfo.value)
    assert session.metadata_deletes == []
    assert session.stamps == []


@pytest.mark.asyncio
async def test_a_discard_stamp_that_matches_no_row_rolls_the_delete_back(
    monkeypatch, tmp_path
):
    """The stamp has to land on exactly the row the transaction locked.

    A delete standing beside a provenance record that does not exist is the
    "rows from one vault beside a record naming another" this branch exists to
    make impossible, so a zero-row stamp aborts the whole transaction rather
    than committing the delete alone.
    """
    old = make_vault(tmp_path, "old", {"Note.md": "old body\n"})
    new = make_vault(tmp_path, "new", {"Fresh.md": "new body\n"})

    class _NoStampSession(FakeSession):
        async def execute(self, stmt, params=None):
            result = await super().execute(stmt, params)
            if isinstance(stmt, Update) and _table_of(stmt) == "users":
                return _Result(rowcount=0)
            return result

    session = _NoStampSession(
        provenance=recorded_from(facts_for(old)),
        existing={"Note.md": hash_of("old body\n")},
        note_ids={"Fresh.md": 10},
        assignment=str(new),
    )
    install(monkeypatch, session, new)

    with pytest.raises(RuntimeError) as excinfo:
        await indexer.index_vault(user_id=7)

    assert "not exactly one" in str(excinfo.value)
    assert session.commits == 0, "the delete was committed without its record"


@pytest.mark.asyncio
async def test_a_re_derive_stamp_is_bound_the_same_way_and_is_withheld(
    monkeypatch, tmp_path
):
    """The tail stamp writes provenance too, so it takes the same binding.

    It is withheld rather than fatal: the re-derive's repairs are correct for
    the root they were read from and nothing was destroyed, so an unrecorded
    provenance simply makes the next pass re-derive again.
    """
    vault = make_vault(tmp_path, "vault", {"Note.md": "body\n"})
    other = make_vault(tmp_path, "other", {})
    session = FakeSession(
        provenance=(None, None, None),
        existing={},
        note_ids={"Note.md": 1},
        assignment=str(other),
    )
    install(monkeypatch, session, vault)
    session.assignment = str(other)

    await indexer.index_vault(user_id=7)

    assert session.assignment_reads == 1
    assert session.stamps == [], "provenance was recorded under another assignment"
    # The repairs still happened and still committed.
    assert session.commits >= 1


@pytest.mark.asyncio
async def test_the_discard_takes_the_users_lock_before_any_child_write(
    monkeypatch, tmp_path
):
    """Lock order, asserted where it is decided (adversarial round 2, MAJOR 5).

    The discard runs in its own transaction and must take the parent row before
    it touches a child — the direction the panel's own user delete takes, so
    the two queue behind each other instead of closing a cycle. It waits for
    that lock (no `NOWAIT`), which is only safe *because* it holds nothing yet.
    """
    old = make_vault(tmp_path, "old", {"Note.md": "old body\n"})
    new_root = make_vault(tmp_path, "new", {"Fresh.md": "new body\n"})
    session = FakeSession(
        provenance=recorded_from(facts_for(old)),
        existing={"Note.md": hash_of("old body\n")},
        note_ids={"Fresh.md": 10},
        assignment=str(new_root),
    )
    install(monkeypatch, session, new_root)

    await indexer.index_vault(user_id=7)

    lock_at = session.timeline.index("lock:users")
    discard_at = session.timeline.index("discard")
    assert lock_at < discard_at, session.timeline
    # It waits, because at that point it holds no child row locks at all.
    assert "lock:users:nowait" not in session.timeline[:discard_at]


@pytest.mark.asyncio
async def test_the_tail_stamp_asks_without_waiting_inside_a_savepoint(
    monkeypatch, tmp_path
):
    """The other half of the lock-order rule.

    The tail runs at the end of the pass's transaction, holding
    `notes_metadata` row locks, and a permanent user delete takes `users` first
    and then cascades onto exactly those rows. Waiting there closes a real
    deadlock cycle, so the tail asks with `NOWAIT` — inside a savepoint,
    because a failed statement poisons its transaction and the repairs must
    survive the refusal.
    """
    vault = make_vault(tmp_path, "vault", {"Note.md": "body\n"})
    session = FakeSession(
        provenance=(None, None, None), existing={}, note_ids={"Note.md": 1}
    )
    install(monkeypatch, session, vault)

    await indexer.index_vault(user_id=7)

    assert "lock:users:nowait" in session.timeline, session.timeline
    assert "lock:users" not in session.timeline, session.timeline
    lock_at = session.timeline.index("lock:users:nowait")
    savepoint_at = session.timeline.index("savepoint")
    stamp_at = session.timeline.index("stamp")
    assert savepoint_at < lock_at < stamp_at, session.timeline
    assert session.timeline[stamp_at + 1] == "savepoint:release", session.timeline
    assert len(session.stamps) == 1


@pytest.mark.asyncio
async def test_a_contended_users_row_withholds_the_stamp_and_keeps_the_repairs(
    monkeypatch, tmp_path
):
    """Adversarial round 2, MAJOR 5 — the failing input, at the tail.

    T2 holds `users` (a permanent delete on its way to the cascade). The old
    code waited, which is the cycle. Now the `NOWAIT` fails, only the savepoint
    rolls back, the stamp is withheld — a state this branch already knows how
    to be in — and the pass still commits every repair it made.
    """
    vault = make_vault(tmp_path, "vault", {"Note.md": "See [[Other]]\n"})
    session = FakeSession(
        provenance=(None, None, None), existing={}, note_ids={"Note.md": 1}
    )
    install(monkeypatch, session, vault)
    session.users_row_locked = True

    await indexer.index_vault(user_id=7)

    assert session.stamps == [], "a contended lock still recorded provenance"
    assert session.timeline[-2:] == ["savepoint:rollback", "commit"], session.timeline
    # The repairs are still there and still committed.
    assert [r["target_path"] for r in session.link_inserts()] == ["Other"]
    assert session.commits >= 1


def test_the_lock_not_available_sqlstate_is_found_through_both_wrappers():
    """Guard the detector: the SQLSTATE lives on asyncpg's own error, two
    layers below what SQLAlchemy raises, exactly as `_log_usage`'s foreign-key
    recovery has to walk. Matching on the class instead would silently degrade
    every contention into a real failure."""
    assert indexer._is_lock_not_available(_LockNotAvailable())
    assert not indexer._is_lock_not_available(RuntimeError("something else"))
    other = Exception("wrong code")
    other.sqlstate = "40P01"
    assert not indexer._is_lock_not_available(other)


@pytest.mark.asyncio
async def test_a_discard_touches_no_other_users_rows(monkeypatch, tmp_path):
    old = make_vault(tmp_path, "old", {"Note.md": "old\n"})
    new = make_vault(tmp_path, "new", {"Fresh.md": "new\n"})
    session = FakeSession(
        provenance=recorded_from(facts_for(old)),
        existing={"Note.md": hash_of("old\n")},
    )
    install(monkeypatch, session, new)

    await indexer.index_vault(user_id=7)

    for statement in session.metadata_deletes:
        rendered = str(statement.compile(compile_kwargs={"literal_binds": True}))
        assert "user_id = 7" in rendered, rendered


@pytest.mark.asyncio
async def test_an_over_long_realpath_still_discards(monkeypatch, tmp_path):
    """The round-4 blocker, end to end and with a real directory tree.

    The user is reassigned to a *short* assignment that is a symbolic link to a
    directory whose canonical real path exceeds `users.vault_path`'s own width.
    With a bounded column the record write raises inside the discard's
    transaction, rolls the delete back with it, and the former vault stays
    queryable on every later pass. Nothing here is mocked: the depth is built
    in the tmpdir.
    """
    old = make_vault(tmp_path, "old", {"Note.md": "old\n"})
    deep = tmp_path / "deep"
    segment = "s" * 40
    while len(str(deep)) <= 1024:
        deep = deep / segment
    deep.mkdir(parents=True)
    (deep / "Fresh.md").write_text("new\n", encoding="utf-8")
    short = tmp_path / "link"
    short.symlink_to(deep, target_is_directory=True)
    assert len(str(short)) < 1024 < len(os.path.realpath(short))

    session = FakeSession(
        provenance=recorded_from(facts_for(old)),
        existing={"Note.md": hash_of("old\n")},
    )
    install(monkeypatch, session, short)

    await indexer.index_vault(user_id=7)

    assert session.timeline.count("discard") == 1
    stamped = session.stamps[0]["indexed_vault_realpath"]
    # Recorded in full — not truncated, not NULL.
    assert indexer.decode_realpath(stamped) == os.path.realpath(short)
    assert len(stamped) > 1024


@pytest.mark.asyncio
async def test_a_non_utf8_realpath_still_discards_and_the_next_pass_keeps(
    monkeypatch, tmp_path
):
    """The round-5 blocker, end to end, with a real non-UTF-8 pathname.

    `os.path.realpath` returns a surrogate-escaped string for such a component,
    which a UTF-8 driver cannot encode at all. The record and the delete are
    one transaction, so that failure would roll the delete back on every later
    pass and serve the former vault forever.

    The second half is what proves the comparison runs on the **encoded** form
    on both sides: a later pass over the same root must classify it *same
    assignment* rather than re-deriving because the two were spelled
    differently.
    """
    try:
        raw = os.path.join(os.fsencode(str(tmp_path)), b"vault-\xff-name")
        os.mkdir(raw)
    except (OSError, ValueError) as e:  # pragma: no cover - filesystem dependent
        pytest.skip(f"this filesystem refuses a non-UTF-8 directory name: {e}")
    new = Path(os.fsdecode(raw))
    (new / "Fresh.md").write_text("new\n", encoding="utf-8")
    assert any("\udc80" <= ch <= "\udcff" for ch in os.path.realpath(new))

    old = make_vault(tmp_path, "old", {"Note.md": "old\n"})
    session = FakeSession(
        provenance=recorded_from(facts_for(old)),
        existing={"Note.md": hash_of("old\n")},
    )
    install(monkeypatch, session, new)

    # No UnicodeEncodeError escapes, and all three columns are stamped.
    await indexer.index_vault(user_id=7)

    assert session.timeline.count("discard") == 1
    stamp = session.stamps[0]
    assert set(stamp) == {
        "indexed_vault_assignment",
        "indexed_vault_realpath",
        "indexed_vault_handle",
    }
    assert indexer.decode_realpath(stamp["indexed_vault_realpath"]) == os.path.realpath(new)

    # The next pass over the same root: same assignment, nothing destroyed.
    second = FakeSession(
        provenance=(
            stamp["indexed_vault_assignment"],
            stamp["indexed_vault_realpath"],
            stamp["indexed_vault_handle"],
        ),
        existing={"Fresh.md": hash_of("new\n")},
    )
    install(monkeypatch, second, new)

    await indexer.index_vault(user_id=7)

    assert "discard" not in second.timeline, second.timeline
    assert second.stamps == [], "a keep stamps nothing"


@pytest.mark.asyncio
async def test_a_pass_that_fails_after_a_discard_is_retried_without_a_second_delete(
    monkeypatch, tmp_path
):
    """The discard commits at the head of the pass, so a scan that then fails
    retries cleanly: the next pass finds both facts in agreement and simply
    indexes."""
    old = make_vault(tmp_path, "old", {"Note.md": "old\n"})
    new = make_vault(tmp_path, "new", {"Fresh.md": "new\n"})
    first = FakeSession(
        provenance=recorded_from(facts_for(old)),
        existing={"Note.md": hash_of("old\n")},
    )
    first.fail_on = lambda stmt: getattr(stmt, "is_insert", False)
    install(monkeypatch, first, new)

    with pytest.raises(RuntimeError):
        await indexer.index_vault(user_id=7)
    assert first.timeline.count("discard") == 1
    stamp = first.stamps[0]

    second = FakeSession(
        provenance=(
            stamp["indexed_vault_assignment"],
            stamp["indexed_vault_realpath"],
            stamp["indexed_vault_handle"],
        ),
        existing={},
    )
    install(monkeypatch, second, new)

    await indexer.index_vault(user_id=7)

    assert "discard" not in second.timeline, second.timeline


@pytest.mark.asyncio
async def test_an_unopenable_root_deletes_nothing_and_records_nothing(
    monkeypatch, tmp_path
):
    """The pass fails; it does not silently prune every row the user had.

    That last part is a change worth stating: `Path.rglob` on a missing
    directory yielded nothing, so the ordinary prune deleted the user's whole
    index for a mount that was briefly away.
    """
    session = FakeSession(provenance=(None, None, None), existing={"Note.md": "h"})
    install(monkeypatch, session, tmp_path / "does-not-exist")

    with pytest.raises(OSError):
        await indexer.index_vault(user_id=7)

    assert session.metadata_deletes == []
    assert session.stamps == []


@pytest.mark.asyncio
async def test_a_root_whose_pathname_moves_under_the_pass_changes_nothing(
    monkeypatch, tmp_path
):
    a = make_vault(tmp_path, "A", {"a.md": "a\n"})
    b = make_vault(tmp_path, "B", {"b.md": "b\n"})
    link = tmp_path / "current"
    link.symlink_to(a, target_is_directory=True)
    session = FakeSession(provenance=(None, None, None), existing={"a.md": "h"})
    install(monkeypatch, session, link)

    real_observe = indexer.observe_root_facts

    def retarget_then_observe(vault, root_fd):
        link.unlink()
        link.symlink_to(b, target_is_directory=True)
        return real_observe(vault, root_fd)

    monkeypatch.setattr(indexer, "observe_root_facts", retarget_then_observe)

    with pytest.raises(RuntimeError, match="indeterminate"):
        await indexer.index_vault(user_id=7)

    assert session.metadata_deletes == []
    assert session.stamps == []


# ══════════════════════════════════════════════════════════════════════════
# Anchoring: the ABA interleaving within one pass
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_the_pass_scans_the_directory_it_pinned_not_the_one_the_link_now_names(
    monkeypatch, tmp_path
):
    """Round 2's blocker.

    The assignment is a symbolic link to A. The link is retargeted to B *after*
    the facts are observed and *before* discovery. Observing through a pathname
    and then scanning that pathname is check-then-act; a directory descriptor
    keeps naming the same directory however its pathname is later relinked, so
    the pass must scan A and any provenance it records must describe A.
    """
    a = make_vault(tmp_path, "A", {"OnlyInA.md": "a body\n"})
    b = make_vault(tmp_path, "B", {"OnlyInB.md": "b body\n"})
    link = tmp_path / "current"
    link.symlink_to(a, target_is_directory=True)
    facts_a = facts_for(link)

    session = FakeSession(provenance=(None, None, None), existing={})
    install(monkeypatch, session, link)

    real_observe = indexer.observe_root_facts

    def observe_then_retarget(vault, root_fd):
        observed = real_observe(vault, root_fd)
        link.unlink()
        link.symlink_to(b, target_is_directory=True)
        return observed

    monkeypatch.setattr(indexer, "observe_root_facts", observe_then_retarget)

    await indexer.index_vault(user_id=7)

    scanned = {row["file_path"] for row in session.metadata_upserts()}
    assert scanned == {"OnlyInA.md"}, "the pass followed the retargeted link"
    # And what it recorded describes the directory it actually scanned.
    assert session.stamps[0]["indexed_vault_realpath"] == facts_a.realpath_hex


def test_discovery_keeps_todays_symbolic_link_behaviour(tmp_path):
    """The anchored walk finds exactly what the pathname walk found.

    Directory symbolic links are not descended — an `ELOOP`/`ENOTDIR` on the
    `O_DIRECTORY | O_NOFOLLOW` descent is a deliberate non-descent, not a skip
    — while a symlinked `.md` is still discovered and read. Anchoring is about
    *which directory is scanned*; it must not change what the index contains.

    `tests/test_symlink_mutation_guard.py` is the other half of this check and
    passes unchanged.
    """
    vault = make_vault(tmp_path, "vault", {"Real/A.md": "a\n", "src.md": "s\n"})
    (vault / "Shared").symlink_to(vault / "Real", target_is_directory=True)
    (vault / "alias.md").symlink_to(vault / "Real" / "A.md")
    (vault / ".obsidian").mkdir()
    (vault / ".obsidian" / "hidden.md").write_text("h\n", encoding="utf-8")

    found = indexer.discover_markdown_files(vault)

    assert sorted(found) == ["Real/A.md", "alias.md", "src.md"]
    assert found["Real/A.md"] == vault / "Real" / "A.md"
    # A symlinked markdown file reads as it is today.
    with indexer.pinned_root(vault) as fd:
        text, _stat = indexer.read_note_beneath(fd, "alias.md")
    assert text == "a\n"


def test_the_walk_costs_one_descriptor_per_level_not_one_per_file(tmp_path):
    """Depth-first, each parent closed once its children are done."""
    notes = {f"d{i}/n{j}.md": "x\n" for i in range(6) for j in range(30)}
    vault = make_vault(tmp_path, "wide", notes)

    open_fds = len(os.listdir("/proc/self/fd"))
    with indexer.pinned_root(vault) as fd:
        peak = 0
        for _found in indexer.discover_markdown_files_at(fd):
            peak = max(peak, len(os.listdir("/proc/self/fd")))
    assert peak - open_fds < 10, f"the walk held {peak - open_fds} descriptors"


# ══════════════════════════════════════════════════════════════════════════
# Driving the pass: re-derive
# ══════════════════════════════════════════════════════════════════════════
#
# The re-derive makes a *structural* claim — after it, every surviving metadata
# row and every link row was written by that pass from a file under the
# assigned root — rather than an enumeration of columns that has to be
# re-audited whenever a column is added. Content-hash change detection is
# disabled, the ordinary prune is unchanged, and because every note counts as
# changed, every one of the user's link rows is deleted and re-extracted.
#
# `note_embeddings` are deliberately **not** deleted: an embedding is a pure
# function of chunk text and `content_hash` establishes content equality, so a
# vector on a row whose hash still matches the file under the assigned root is
# provably that file's vector. That is why a re-derive costs no embedding call
# for unchanged content while a discard costs a full re-embed.


@pytest.mark.asyncio
async def test_a_null_record_re_derives_rather_than_discarding_or_trusting(
    monkeypatch, tmp_path
):
    """The legacy population, and the reason introducing the columns costs no
    vault-wide re-embed."""
    body = "unchanged body\n"
    vault = make_vault(tmp_path, "vault", {"Note.md": body})
    session = FakeSession(
        provenance=(None, None, None),
        existing={"Note.md": hash_of(body)},
        note_ids={"Note.md": 1},
    )
    install(monkeypatch, session, vault)

    await indexer.index_vault(user_id=7)

    assert "discard" not in session.timeline, session.timeline
    # Change detection is off: the note is re-parsed and re-upserted even
    # though its hash still matches.
    assert [row["file_path"] for row in session.metadata_upserts()] == ["Note.md"]
    # And the embeddings are untouched.
    assert not any(
        isinstance(s, Delete) and _table_of(s) == "note_embeddings"
        for s in session.statements
    )


@pytest.mark.asyncio
async def test_a_legacy_index_built_from_a_different_vault_is_repaired(
    monkeypatch, tmp_path
):
    """The reviewer's case, verbatim.

    Vault A holds `Same.md` linking to `OnlyA.md`. Vault B holds a
    byte-identical `Same.md` and no `OnlyA.md`. The user was indexed on A,
    reassigned to B before any record existed, and the first pass after the
    upgrade has to repair it.

    The ordinary prune alone does not: `Same.md`'s relative path *and* content
    hash are identical in both roots, so the incremental scan classes it "no
    change" and never re-extracts its links — while `OnlyA.md` is pruned and
    `note_links.target_note_id` is `ON DELETE SET NULL`, leaving a resolved
    link silently dangling **forever**, because the source's hash keeps
    matching. Re-derive mode is what re-extracts it.
    """
    same = "See [[OnlyA]]\n"
    make_vault(tmp_path, "A", {"Same.md": same, "OnlyA.md": "only in A\n"})
    b = make_vault(tmp_path, "B", {"Same.md": same})

    session = FakeSession(
        provenance=(None, None, None),
        existing={"Same.md": hash_of(same), "OnlyA.md": hash_of("only in A\n")},
        # The vault index as it stands after the prune: B has no `OnlyA.md`.
        note_ids={"Same.md": 1},
    )
    install(monkeypatch, session, b)

    await indexer.index_vault(user_id=7)

    # No row remains whose relative path is absent under the assigned root.
    assert "prune" in session.timeline, session.timeline
    pruned = str(
        next(
            s for s in session.metadata_deletes
            if "file_path" in str(s)
        ).compile(compile_kwargs={"literal_binds": True})
    )
    assert "OnlyA.md" in pruned

    # `Same.md`'s links were deleted and re-extracted from B, resolved against
    # B alone — so the link to `OnlyA` is dangling rather than silently
    # retaining A's resolution.
    assert session.note_link_deletes(), "the link rows were not re-extracted"
    rows = session.link_inserts()
    assert [r["target_path"] for r in rows] == ["OnlyA"]
    assert rows[0]["target_note_id"] is None
    assert rows[0]["source_note_id"] == 1


@pytest.mark.asyncio
async def test_a_complete_re_derive_stamps_all_three_and_the_next_pass_no_ops(
    monkeypatch, tmp_path
):
    body = "body\n"
    vault = make_vault(tmp_path, "vault", {"Note.md": body})
    first = FakeSession(
        provenance=(None, None, None),
        existing={"Note.md": hash_of(body)},
        note_ids={"Note.md": 1},
    )
    install(monkeypatch, first, vault)

    await indexer.index_vault(user_id=7)

    # Stamped after the pass's last write, and committed with it.
    assert len(first.stamps) == 1
    assert first.timeline.index("stamp") < first.timeline.index("commit")
    stamp = first.stamps[0]
    assert set(stamp) == {
        "indexed_vault_assignment",
        "indexed_vault_realpath",
        "indexed_vault_handle",
    }

    second = FakeSession(
        provenance=(
            stamp["indexed_vault_assignment"],
            stamp["indexed_vault_realpath"],
            stamp["indexed_vault_handle"],
        ),
        existing={"Note.md": hash_of(body)},
        note_ids={"Note.md": 1},
    )
    install(monkeypatch, second, vault)

    await indexer.index_vault(user_id=7)

    assert second.stamps == [], "the no-op branch must stamp nothing"
    assert second.metadata_upserts() == [], "the no-op branch must re-upsert nothing"


@pytest.mark.asyncio
async def test_a_re_derive_that_raises_stamps_nothing(monkeypatch, tmp_path):
    vault = make_vault(tmp_path, "vault", {"Note.md": "body\n"})
    session = FakeSession(provenance=(None, None, None), existing={})
    session.fail_on = lambda stmt: getattr(stmt, "is_insert", False)
    install(monkeypatch, session, vault)

    with pytest.raises(RuntimeError):
        await indexer.index_vault(user_id=7)

    assert session.stamps == []


@pytest.mark.asyncio
async def test_the_stamp_is_whole_even_when_a_fact_is_unobservable(
    monkeypatch, tmp_path
):
    """Every stamp writes all three columns, NULL for anything not observed.

    A branch that updated one column and left another describing a root it does
    not describe would let a later observation be compared against a root the
    stamp never covered. In particular a handle-less stamp must NULL a
    previously recorded handle rather than leave it standing beside a freshly
    observed pathname pair — and the pass after it must then take the *keep*
    branch (no recorded handle means no observable mismatch), not a discard.
    """
    vault = make_vault(tmp_path, "vault", {"Note.md": "body\n"})
    monkeypatch.setattr(indexer, "read_dir_handle", lambda _fd: None)
    facts = facts_for(vault)
    session = FakeSession(
        provenance=(facts.assignment, "00", "1:staleandwrong"),
        existing={},
        note_ids={},
    )
    install(monkeypatch, session, vault)

    await indexer.index_vault(user_id=7)

    assert session.stamps[0]["indexed_vault_handle"] is None
    # A later, handle-capable pass over that same root keeps.
    monkeypatch.undo()
    install(monkeypatch, session, vault)
    stamp = session.stamps[0]
    verdict = indexer.classify_provenance(
        stamp["indexed_vault_assignment"],
        stamp["indexed_vault_realpath"],
        stamp["indexed_vault_handle"],
        facts_for(vault),
    )
    assert verdict.verdict == indexer.PROVENANCE_KEEP


# ══════════════════════════════════════════════════════════════════════════
# Completeness: any per-file skip withholds the stamp
# ══════════════════════════════════════════════════════════════════════════
#
# The re-derive's claim is only true if the pass actually visited every file.
# The scan continues past a file it cannot decode or read, and the ordinary
# prune keeps a row whose relative path exists under the assigned root — which
# is exactly the row a re-derive exists to replace. One skipped file is enough
# to certify a foreign row, so a skip withholds the stamp.
#
# The rule fails toward re-work rather than toward wrongness. The alternative —
# transactionally deleting the stale rows for each skipped path — is a second
# deletion path for index contents, and it destroys a row that may be the
# correct row for a file that was merely unreadable at that moment.


@pytest.mark.asyncio
async def test_an_undecodable_file_withholds_the_stamp(monkeypatch, tmp_path, caplog):
    """Real invalid UTF-8 bytes, not a mocked decoder.

    Vault A supplied `Same.md`; vault B holds a `Same.md` whose bytes cannot be
    decoded. The path is discovered, the read raises, and the row and its links
    survive untouched — so the pass must not certify B over them.
    """
    vault = make_vault(tmp_path, "B", {"Fine.md": "fine\n"})
    (vault / "Same.md").write_bytes(b"\xff\xfe not utf-8 at all\n")
    session = FakeSession(
        provenance=(None, None, None),
        existing={"Same.md": "hash-from-vault-A", "Fine.md": hash_of("fine\n")},
        note_ids={"Fine.md": 2},
    )
    install(monkeypatch, session, vault)

    with caplog.at_level("WARNING"):
        await indexer.index_vault(user_id=7)

    assert session.stamps == [], "an incomplete re-derive must not be recorded"
    # The repairs it *could* do were still done.
    assert [row["file_path"] for row in session.metadata_upserts()] == ["Fine.md"]
    # Nothing was invented to replace A's row for the skipped path: it was
    # discovered, so the ordinary prune leaves it alone.
    for statement in session.metadata_deletes:
        assert "Same.md" not in str(
            statement.compile(compile_kwargs={"literal_binds": True})
        )
    # And the offending path is named, on every pass, so the file to fix is
    # identified rather than left as an unexplained recurring cost.
    assert "Same.md" in caplog.text
    assert "Re-derive incomplete" in caplog.text

    # The next pass re-derives again rather than keeping.
    again = FakeSession(
        provenance=(None, None, None),
        existing={"Same.md": "hash-from-vault-A", "Fine.md": hash_of("fine\n")},
        note_ids={"Fine.md": 2},
    )
    install(monkeypatch, again, vault)
    await indexer.index_vault(user_id=7)
    assert again.stamps == []


@pytest.mark.asyncio
async def test_a_file_that_disappears_during_the_scan_is_a_skip(monkeypatch, tmp_path):
    vault = make_vault(tmp_path, "vault", {"Gone.md": "x\n", "Kept.md": "y\n"})
    session = FakeSession(provenance=(None, None, None), existing={}, note_ids={})
    install(monkeypatch, session, vault)

    real = indexer.read_note_at

    def vanish(parent_fd, name):
        if name == "Gone.md":
            raise FileNotFoundError(errno.ENOENT, "vanished", name)
        return real(parent_fd, name)

    monkeypatch.setattr(indexer, "read_note_at", vanish)

    await indexer.index_vault(user_id=7)

    assert session.stamps == []
    assert [row["file_path"] for row in session.metadata_upserts()] == ["Kept.md"]


@pytest.mark.asyncio
async def test_a_note_deleted_between_the_scan_and_the_link_rebuild_is_not_a_skip(
    monkeypatch, tmp_path
):
    """The link rebuild reads no file, so this window no longer exists.

    It used to re-read each changed note from disk — a second read of bytes the
    scan had already parsed, and a second window in which the file could vanish
    and silently drop that note's links while the row the scan wrote stood. It
    now extracts from the scan's own buffer.
    """
    vault = make_vault(tmp_path, "vault", {"Note.md": "See [[Other]]\n"})
    session = FakeSession(
        provenance=(None, None, None), existing={}, note_ids={"Note.md": 1}
    )
    install(monkeypatch, session, vault)

    real = indexer._update_links_for_changed

    async def delete_then_rebuild(*args, **kwargs):
        (vault / "Note.md").unlink()
        return await real(*args, **kwargs)

    monkeypatch.setattr(indexer, "_update_links_for_changed", delete_then_rebuild)

    await indexer.index_vault(user_id=7)

    assert [r["target_path"] for r in session.link_inserts()] == ["Other"]
    # Not a skip, so a complete pass still stamps.
    assert len(session.stamps) == 1


@pytest.mark.asyncio
async def test_a_changed_path_with_no_index_row_is_recorded_as_a_skip(
    monkeypatch, tmp_path
):
    """The one link-extraction skip that used to be silent.

    `_update_links_for_changed` drops a changed path whose `paths_to_id` lookup
    comes back empty. Its sibling — a changed path with no buffered body —
    records a skip, and A.7a's rule is that *any* such skip withholds the
    re-derive's certification. Recording only one of the two left the single
    branch that can drop a link row while still stamping "every link row was
    written by this pass". Practically unreachable, since the index is selected
    after the upsert in the same transaction; recorded anyway, because the
    stamp is a claim about completeness.
    """
    vault = make_vault(tmp_path, "vault", {"a.md": "see [[x]]\n", "b.md": "b\n"})
    session = FakeSession(
        provenance=(None, None, None), existing={}, note_ids={"a.md": 1}
    )
    install(monkeypatch, session, vault)

    await indexer.index_vault(user_id=7)

    assert session.stamps == [], "an incomplete re-derive recorded provenance"


@pytest.mark.asyncio
async def test_a_pass_with_an_empty_skip_list_stamps(monkeypatch, tmp_path):
    vault = make_vault(tmp_path, "vault", {"a.md": "a\n", "sub/b.md": "b\n"})
    session = FakeSession(provenance=(None, None, None), existing={}, note_ids={})
    install(monkeypatch, session, vault)

    await indexer.index_vault(user_id=7)

    assert len(session.stamps) == 1


# ══════════════════════════════════════════════════════════════════════════
# The declared non-goal
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_a_substituted_root_behind_an_unchanged_assignment_is_kept(
    monkeypatch, tmp_path
):
    """**This asserts the documented consequence, not a prevention. Do not
    "fix" it.**

    The system does not detect, and does not claim to detect, a change of
    *storage* underneath an unchanged vault assignment. It is unwinnable by
    construction — a bit-identical clone presents the same inode numbers,
    generation counters and therefore the same file handles, at the same
    pathname — and it is the same trust class as editing the database directly.

    What is promised is the bound: a keep is not a no-op. The ordinary scan
    reconciles by relative path and content hash and prunes the paths the
    substitute lacks. The one case that does not heal — a note identical by
    path *and* hash keeping a dangling link — is a **pre-existing defect of the
    incremental indexer**, reachable today on a single vault with no
    reassignment anywhere in the story, and its fix belongs to link resolution.
    """
    same = "See [[OnlyBefore]]\n"
    vault = make_vault(
        tmp_path, "vault", {"Same.md": same, "OnlyBefore.md": "before\n"}
    )
    facts = facts_for(vault)

    # The substitution: same pathname, same assignment, same recorded facts.
    (vault / "OnlyBefore.md").unlink()

    session = FakeSession(
        provenance=(facts.assignment, facts.realpath_hex, facts.handle),
        existing={"Same.md": hash_of(same), "OnlyBefore.md": hash_of("before\n")},
        note_ids={"Same.md": 1},
    )
    install(monkeypatch, session, vault)

    await indexer.index_vault(user_id=7)

    # Kept — no discard, no re-derive, no stamp.
    assert "discard" not in session.timeline
    assert session.stamps == []
    # And reconciled by the ordinary scan: the absent path is pruned.
    pruned = str(
        next(s for s in session.metadata_deletes).compile(
            compile_kwargs={"literal_binds": True}
        )
    )
    assert "OnlyBefore.md" in pruned
    # The documented residual: `Same.md` is unchanged by path and hash, so it
    # is never re-parsed and its link row keeps a null resolution.
    assert session.metadata_upserts() == []
    assert session.link_inserts() == []


# ══════════════════════════════════════════════════════════════════════════
# The gated ancillary passes
# ══════════════════════════════════════════════════════════════════════════
#
# `link_backfill_pass` and `rebuild_tsvectors` both read `vault / file_path`
# and write rows the provenance is a claim about — `note_links`,
# `content_tsvector` — with **no verification of any kind** that the bytes they
# read belong to the row they write against. Neither may assume the scan
# settled that claim a moment ago: a user whose notes contain no links leaves
# the backfill eligible on *every* startup, and a reassignment can commit
# between the scan and either of them.
#
# Verification is not merely unimplemented in those two. A link row's
# *resolution* is a function of the whole set of notes under a root, so no
# per-file check could license the backfill; and nothing records what a
# tsvector was built from, so a vector built from foreign bytes leaves no
# evidence a later pass could act on. Skipping costs them nothing, because the
# re-derive branch does both of their jobs itself on every pass.


@pytest.mark.asyncio
async def test_the_link_backfill_writes_nothing_for_an_unsettled_user(
    monkeypatch, tmp_path, caplog
):
    vault = make_vault(tmp_path, "vault", {"Note.md": "See [[Other]]\n"})
    session = FakeSession(provenance=(None, None, None), note_ids={"Note.md": 1})
    install(monkeypatch, session, vault)

    with caplog.at_level("INFO"):
        await indexer.link_backfill_pass(user_id=7)

    assert session.link_inserts() == []
    assert session.note_link_deletes() == []
    assert session.commits == 0
    assert "Link backfill skipped for user_id=7" in caplog.text


@pytest.mark.asyncio
async def test_the_link_backfill_proceeds_for_a_settled_user(monkeypatch, tmp_path):
    vault = make_vault(tmp_path, "vault", {"Note.md": "See [[Other]]\n"})
    facts = facts_for(vault)
    session = FakeSession(
        provenance=(facts.assignment, facts.realpath_hex, facts.handle),
        note_ids={"Note.md": 1},
    )
    install(monkeypatch, session, vault)

    await indexer.link_backfill_pass(user_id=7)

    assert [r["target_path"] for r in session.link_inserts()] == ["Other"]


@pytest.mark.asyncio
async def test_the_skip_is_per_user_not_global(monkeypatch, tmp_path):
    """One unsettled user must not stop the pass for everybody else."""
    vault = make_vault(tmp_path, "vault", {"Note.md": "See [[Other]]\n"})
    facts = facts_for(vault)

    unsettled = FakeSession(provenance=(None, None, None), note_ids={"Note.md": 1})
    install(monkeypatch, unsettled, vault)
    await indexer.link_backfill_pass(user_id=7)

    settled = FakeSession(
        provenance=(facts.assignment, facts.realpath_hex, facts.handle),
        note_ids={"Note.md": 1},
    )
    install(monkeypatch, settled, vault)
    await indexer.link_backfill_pass(user_id=8)

    assert unsettled.link_inserts() == []
    assert settled.link_inserts() != []


@pytest.mark.asyncio
async def test_a_reassignment_between_the_scan_and_the_backfill_writes_nothing(
    monkeypatch, tmp_path
):
    """Round 3's failing input.

    The user is reassigned from A to B between `index_vault` and
    `link_backfill_pass`, and both vaults hold `Same.md` with *different* link
    targets. Without the gate, a link row extracted from B lands against a
    metadata row derived from A.
    """
    a = make_vault(tmp_path, "A", {"Same.md": "See [[TargetInA]]\n"})
    make_vault(tmp_path, "B", {"Same.md": "See [[TargetInB]]\n"})
    b = tmp_path / "B"

    session = FakeSession(
        provenance=recorded_from(facts_for(a)), note_ids={"Same.md": 1}
    )
    install(monkeypatch, session, b)

    await indexer.link_backfill_pass(user_id=7)

    assert session.link_inserts() == []
    assert session.note_link_deletes() == []


@pytest.mark.asyncio
async def test_rebuild_tsvectors_writes_nothing_for_an_unsettled_user(
    monkeypatch, tmp_path, caplog
):
    vault = make_vault(tmp_path, "vault", {"Note.md": "body\n"})
    session = FakeSession(provenance=(None, None, None), note_ids={"Note.md": 1})
    monkeypatch.setattr(indexer, "_vault_root", lambda _uid: vault)

    with caplog.at_level("INFO"):
        updated = await indexer.rebuild_tsvectors(session, user_id=7)

    assert updated == 0
    assert not any(isinstance(s, TextClause) for s in session.statements)
    assert "Keyword-vector rebuild skipped for user_id=7" in caplog.text


@pytest.mark.asyncio
async def test_rebuild_tsvectors_proceeds_for_a_settled_user(monkeypatch, tmp_path):
    vault = make_vault(tmp_path, "vault", {"Note.md": "body\n"})
    facts = facts_for(vault)
    session = FakeSession(
        provenance=(facts.assignment, facts.realpath_hex, facts.handle),
        note_ids={"Note.md": 1},
    )
    monkeypatch.setattr(indexer, "_vault_root", lambda _uid: vault)

    updated = await indexer.rebuild_tsvectors(session, user_id=7)

    assert updated == 1


@pytest.mark.asyncio
async def test_single_user_mode_neither_reads_nor_writes_the_record(
    monkeypatch, tmp_path
):
    """Single-user mode has no `users` row, so the classification is skipped
    entirely — and the environment-derived `settings.vault_path` therefore
    never reaches the assignment column."""
    vault = make_vault(tmp_path, "vault", {"Note.md": "See [[Other]]\n"})
    session = FakeSession(
        provenance=(None, None, None), existing={}, note_ids={"Note.md": 1}
    )
    install(monkeypatch, session, vault)

    await indexer.index_vault(user_id=None)

    # Neither read nor written.
    assert session.stamps == []
    assert not any(
        isinstance(stmt, Select) and "indexed_vault_assignment" in str(stmt)
        for stmt in session.statements
    )

    backfill = FakeSession(
        provenance=(None, None, None), existing={}, note_ids={"Note.md": 1}
    )
    install(monkeypatch, backfill, vault)

    await indexer.link_backfill_pass(user_id=None)

    assert backfill.stamps == []
    # The backfill is ungated here, so it did its work rather than skipping a
    # user that does not exist.
    assert [r["target_path"] for r in backfill.link_inserts()] == ["Other"]


# ══════════════════════════════════════════════════════════════════════════
# The embedding pass: deliberately NOT gated, because it verifies
# ══════════════════════════════════════════════════════════════════════════


class EmbedSession:
    """Just enough session for `embed_vault`."""

    def __init__(self, rows):
        self.rows = list(rows)
        self.marked: list[tuple[str, int]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def commit(self):
        pass

    async def rollback(self):
        pass

    async def execute(self, stmt, params=None):
        if isinstance(stmt, TextClause):
            if "embedded_content_hash IS NULL" in stmt.text:
                return _Result(self.rows)
            if "SET embedded_content_hash" in stmt.text:
                self.marked.append((params["h"], params["i"]))
                return _Result()
            return _Result()
        if isinstance(stmt, Select):
            note_id = stmt.compile().params.get("id_1")
            row = next(r for r in self.rows if r.id == note_id)
            result = _Result([SimpleNamespace(
                id=row.id,
                file_path=row.file_path,
                content_hash=row.content_hash,
                embedded_content_hash=None,
            )])
            result.scalar_one = lambda: result.rows[0]
            return result
        return _Result()


@pytest.mark.asyncio
async def test_embed_vault_refuses_to_certify_content_it_did_not_read(
    monkeypatch, tmp_path
):
    """`embed_note` marks a row embedded by copying the **row's**
    `content_hash`, not a hash of the bytes it embedded.

    So a file that differs from its row at embedding time would be embedded and
    then permanently marked as embedded for a hash it does not have, and
    nothing would ever re-embed it. This check is what makes the re-derive's
    retention of `note_embeddings` sound, and it is the **entire licence** for
    running this pass ungated on provenance.
    """
    vault = make_vault(tmp_path, "vault", {"Note.md": "the bytes on disk\n"})
    row = SimpleNamespace(
        id=1, file_path="Note.md", content_hash="a-hash-of-something-else"
    )
    session = EmbedSession([row])
    monkeypatch.setattr(indexer, "async_session", lambda: session)
    monkeypatch.setattr(indexer, "_vault_root", lambda _uid: vault)
    monkeypatch.setattr(indexer.settings, "embedding_exclude_patterns", [], raising=False)
    monkeypatch.setattr(indexer, "_is_paused", lambda: False)

    embedded = []

    async def fake_embed_note(session, note, content, **kwargs):
        embedded.append((note.file_path, kwargs.get("certified_hash")))
        return 1

    monkeypatch.setattr(indexer, "embed_note", fake_embed_note)

    await indexer.embed_vault(user_id=7)

    assert embedded == [], "content that does not hash to the row was embedded"
    assert session.marked == [], "the row was marked embedded anyway"

    # A later pass, after the scan has refreshed the row, embeds it — and
    # certifies against the hash the bytes were verified against, never one
    # re-read from the row.
    row.content_hash = hash_of("the bytes on disk\n")
    await indexer.embed_vault(user_id=7)
    assert embedded == [("Note.md", hash_of("the bytes on disk\n"))]


class CertifyingSession:
    """`embed_vault` against the **real** `embed_note`, with a row that can move.

    `current` is what `notes_metadata` holds *now*: the ORM re-read answers
    from it, and the conditional certification matches a row only when its
    predicate agrees with it. That is the whole interleaving — the pass
    verified bytes against `selected`, and by the time it certifies the row may
    say something else.
    """

    def __init__(self, selected, current_hash=None, current_path=None):
        self.selected = selected
        self.current_hash = current_hash or selected.content_hash
        self.current_path = current_path or selected.file_path
        self.certified: list[tuple[str, str]] = []
        self.vector_deletes = 0
        self.added: list = []
        self.commits = 0
        self.rollbacks = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1

    async def flush(self):
        pass

    def add(self, obj):
        self.added.append(obj)

    def expire(self, _obj, _attrs=None):
        pass

    async def execute(self, stmt, params=None):
        if isinstance(stmt, TextClause):
            if "embedded_content_hash IS NULL" in stmt.text:
                return _Result([self.selected])
            return _Result()
        if isinstance(stmt, Update) and _table_of(stmt) == "notes_metadata":
            rendered = str(stmt.compile(compile_kwargs={"literal_binds": True}))
            values = {
                getattr(k, "name", str(k)): _literal(v)
                for k, v in dict(stmt._values or {}).items()
            }
            stamped = values["embedded_content_hash"]
            # The conditional predicate is evaluated against the row as it is
            # *now*, exactly as PostgreSQL re-evaluates an UPDATE's WHERE after
            # taking the row lock under READ COMMITTED.
            matches = (
                repr(self.current_hash)[1:-1] in rendered
                and repr(self.current_path)[1:-1] in rendered
            )
            if not matches:
                return _Result(rowcount=0)
            self.certified.append((self.current_path, stamped))
            return _Result(rowcount=1)
        if isinstance(stmt, Delete) and _table_of(stmt) == "note_embeddings":
            self.vector_deletes += 1
            return _Result()
        if isinstance(stmt, Select):
            # The ORM re-read: it sees the row as it is now, which is the whole
            # point of the failing input.
            note = SimpleNamespace(
                id=self.selected.id,
                file_path=self.current_path,
                content_hash=self.current_hash,
                embedded_content_hash=None,
            )
            result = _Result([note])
            result.scalar_one = lambda: note
            return result
        return _Result()


@pytest.mark.asyncio
async def test_embed_vault_applies_no_provenance_gate_at_all(monkeypatch, tmp_path):
    """Round 4's correction, asserted rather than decorated.

    Gating this pass composed with the completeness rule into indefinite
    staleness: a permanently unreadable file withholds the provenance record
    forever — by design — and the gate would have turned that into a permanent
    refusal to embed *anything* for that user, while `semantic_search` went on
    returning the chunk text of content a readable note no longer has.

    The previous version of this test parametrised a `classification` string
    that changed no fixture, mock, row or code path, so both cases were
    identical and it could not have detected a gate. This asserts the property
    directly: `embed_vault` reads no provenance and calls no classifier, under
    any recorded state.
    """
    body = "fresh body\n"
    vault = make_vault(tmp_path, "vault", {"Note.md": body})
    row = SimpleNamespace(id=1, file_path="Note.md", content_hash=hash_of(body))
    session = EmbedSession([row])
    monkeypatch.setattr(indexer, "async_session", lambda: session)
    monkeypatch.setattr(indexer, "_vault_root", lambda _uid: vault)
    monkeypatch.setattr(indexer.settings, "embedding_exclude_patterns", [], raising=False)
    monkeypatch.setattr(indexer, "_is_paused", lambda: False)

    def refuse(*_a, **_kw):  # pragma: no cover - fails the test if reached
        raise AssertionError("embed_vault consulted the provenance record")

    monkeypatch.setattr(indexer, "classify_for_pass", refuse)
    monkeypatch.setattr(indexer, "_read_recorded_provenance", refuse)
    monkeypatch.setattr(indexer, "_reconcile_provenance", refuse)

    embedded = []

    async def fake_embed_note(session, note, content, **kwargs):
        embedded.append((note.file_path, kwargs.get("certified_hash")))
        return 1

    monkeypatch.setattr(indexer, "embed_note", fake_embed_note)

    await indexer.embed_vault(user_id=7)

    assert embedded == [("Note.md", hash_of(body))]
    assert not any(
        "indexed_vault_assignment" in str(stmt)
        for stmt in getattr(session, "statements", [])
    )


@pytest.mark.asyncio
async def test_a_row_that_changes_between_verification_and_certification_is_refused(
    monkeypatch, tmp_path
):
    """The reviewer's failing input, exactly.

    The initial query returns note 1 with hash H1; the pass reads content C1
    and verifies `hash(C1) == H1`. Before the ORM re-read, another index
    transaction commits note 1 with hash H2. The re-read therefore returns the
    H2 row — and the old code handed *that* value to `embed_note`, which
    stamped it onto vectors built from C1. Semantic search then returned C1's
    chunks while the metadata and the disk described C2, and the equality
    H2 == H2 blocked every later re-embed: permanently wrong results for a
    consumer that acts on them without a human seeing the query.

    Now the certification is a conditional write against **H1** — the hash of
    what was actually embedded — so a row that moved matches nothing, the
    vectors are discarded, and the row is left unmarked for a later pass.
    """
    body = "the bytes this pass read\n"
    vault = make_vault(tmp_path, "vault", {"Note.md": body})
    h1 = hash_of(body)
    h2 = hash_of("what another pass committed\n")

    selected = SimpleNamespace(id=1, file_path="Note.md", content_hash=h1)
    session = CertifyingSession(selected, current_hash=h2)
    monkeypatch.setattr(indexer, "async_session", lambda: session)
    monkeypatch.setattr(indexer, "_vault_root", lambda _uid: vault)
    monkeypatch.setattr(indexer.settings, "embedding_exclude_patterns", [], raising=False)
    monkeypatch.setattr(indexer, "_is_paused", lambda: False)

    async def fake_batch(chunks):
        return [[0.0] * 4 for _ in chunks]

    monkeypatch.setattr(embeddings, "get_embeddings_batch", fake_batch)

    await indexer.embed_vault(user_id=7)

    assert session.certified == [], "stale vectors were certified"
    assert session.vector_deletes == 0, "the old vectors were dropped anyway"
    assert session.added == [], "stale vectors were inserted"
    assert session.rollbacks == 1


@pytest.mark.asyncio
async def test_the_exclusion_branch_is_certified_like_the_embedding_path(
    monkeypatch, tmp_path
):
    """Adversarial round 2, MAJOR 4 — the reviewer's exact interleaving.

    id=42 is selected as `Private/A.md` with hash H while `Private/**` is
    excluded. Before the branch runs, `move_note` commits the same row as
    `Public/A.md` with **the same H** (a move changes no content). The old
    branch stamped by `id` alone: it deleted the note's vectors and marked the
    now-*included* `Public/A.md` embedded with none. `content_hash ==
    embedded_content_hash`, so no later pass ever selects it again and
    `semantic_search` omits it silently and permanently.

    The certification predicate includes the path, so the moved row matches
    nothing, `StaleCertification` rolls the note back, and the vectors it would
    have deleted stay.
    """
    vault = make_vault(tmp_path, "vault", {"Private/A.md": "body\n"})
    h = hash_of("body\n")
    selected = SimpleNamespace(id=42, file_path="Private/A.md", content_hash=h)
    # Committed by a concurrent move before the branch runs.
    session = CertifyingSession(selected, current_path="Public/A.md", current_hash=h)
    monkeypatch.setattr(indexer, "async_session", lambda: session)
    monkeypatch.setattr(indexer, "_vault_root", lambda _uid: vault)
    monkeypatch.setattr(
        indexer.settings, "embedding_exclude_patterns", ["Private/*"], raising=False
    )
    monkeypatch.setattr(indexer, "_is_paused", lambda: False)

    await indexer.embed_vault(user_id=7)

    assert session.certified == [], "a moved row was stamped by id alone"
    assert session.vector_deletes == 0, "the vectors were dropped anyway"
    assert session.rollbacks == 1


@pytest.mark.asyncio
async def test_an_unmoved_excluded_row_is_still_stamped_and_its_vectors_dropped(
    monkeypatch, tmp_path
):
    """The positive control: the exclusion branch still does its job.

    Without this the test above would pass against a branch that certifies
    nothing and therefore never drops a stale vector either."""
    vault = make_vault(tmp_path, "vault", {"Private/A.md": "body\n"})
    h = hash_of("body\n")
    selected = SimpleNamespace(id=42, file_path="Private/A.md", content_hash=h)
    session = CertifyingSession(selected)
    monkeypatch.setattr(indexer, "async_session", lambda: session)
    monkeypatch.setattr(indexer, "_vault_root", lambda _uid: vault)
    monkeypatch.setattr(
        indexer.settings, "embedding_exclude_patterns", ["Private/*"], raising=False
    )
    monkeypatch.setattr(indexer, "_is_paused", lambda: False)

    await indexer.embed_vault(user_id=7)

    assert session.certified == [("Private/A.md", h)]
    assert session.vector_deletes == 1
    assert session.rollbacks == 0
    # And it never read the file: the exclusion branch embeds nothing.
    assert session.added == []


@pytest.mark.asyncio
async def test_an_unchanged_row_is_certified_against_what_was_embedded(
    monkeypatch, tmp_path
):
    """The positive control, and the second half of the rule: the hash written
    is the one the bytes were verified against, never one re-read from the row.

    Without this the test above would pass against an `embed_note` that
    certifies nothing at all."""
    body = "the bytes this pass read\n"
    vault = make_vault(tmp_path, "vault", {"Note.md": body})
    h1 = hash_of(body)

    selected = SimpleNamespace(id=1, file_path="Note.md", content_hash=h1)
    session = CertifyingSession(selected)
    monkeypatch.setattr(indexer, "async_session", lambda: session)
    monkeypatch.setattr(indexer, "_vault_root", lambda _uid: vault)
    monkeypatch.setattr(indexer.settings, "embedding_exclude_patterns", [], raising=False)
    monkeypatch.setattr(indexer, "_is_paused", lambda: False)

    async def fake_batch(chunks):
        return [[0.0] * 4 for _ in chunks]

    monkeypatch.setattr(embeddings, "get_embeddings_batch", fake_batch)

    await indexer.embed_vault(user_id=7)

    assert session.certified == [("Note.md", h1)]
    assert session.vector_deletes == 1
    assert len(session.added) == 1
    assert session.rollbacks == 0


@pytest.mark.asyncio
async def test_one_unreadable_note_does_not_freeze_a_readable_notes_vectors(
    monkeypatch, tmp_path
):
    """Round 4's failing input, both halves in one pass.

    A vault holding one permanently unreadable note and one readable note whose
    content is then changed: no provenance is ever recorded for that user
    (correct — the re-derive is incomplete), *and* the changed note's
    embeddings are still updated. A single bad file must not freeze a readable
    note's vectors at content it no longer has.
    """
    body = "changed body\n"
    vault = make_vault(tmp_path, "vault", {"Readable.md": body})
    (vault / "Broken.md").write_bytes(b"\xff\xfe not utf-8\n")

    scan = FakeSession(
        provenance=(None, None, None),
        existing={"Readable.md": "an older hash", "Broken.md": "an older hash"},
        note_ids={"Readable.md": 1},
    )
    install(monkeypatch, scan, vault)

    await indexer.index_vault(user_id=7)

    assert scan.stamps == [], "an incomplete re-derive must record nothing"

    # And the embedding pass, which is not gated, still updates the readable
    # note — whose row the scan has just refreshed to the on-disk hash.
    row = SimpleNamespace(id=1, file_path="Readable.md", content_hash=hash_of(body))
    embed = EmbedSession([row])
    monkeypatch.setattr(indexer, "async_session", lambda: embed)
    monkeypatch.setattr(indexer.settings, "embedding_exclude_patterns", [], raising=False)
    monkeypatch.setattr(indexer, "_is_paused", lambda: False)

    embedded = []

    async def fake_embed_note(session, note, content, **kwargs):
        embedded.append(note.file_path)
        return 1

    monkeypatch.setattr(indexer, "embed_note", fake_embed_note)

    await indexer.embed_vault(user_id=7)

    assert embedded == ["Readable.md"]


# ══════════════════════════════════════════════════════════════════════════
# Inheritance
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_every_caller_of_the_index_pass_inherits_the_reconciliation(
    monkeypatch, tmp_path
):
    """The reconciliation lives in the pass rather than in any one caller, so
    the startup pass, the periodic tick and an operator-triggered reindex all
    get it. Asserted by driving the tick's own per-user entry point."""
    vault = make_vault(tmp_path, "vault", {"Note.md": "body\n"})
    calls: list[int | None] = []
    real = indexer._reconcile_provenance

    async def recording(user_id, vault_, root_fd, log_suffix):
        calls.append(user_id)
        return await real(user_id, vault_, root_fd, log_suffix)

    monkeypatch.setattr(indexer, "_reconcile_provenance", recording)

    async def noop_embed(user_id=None):
        return None

    monkeypatch.setattr(indexer, "embed_vault", noop_embed)
    session = FakeSession(provenance=(None, None, None), existing={}, note_ids={})
    install(monkeypatch, session, vault)

    assert await indexer._index_pass_once(7) is True

    assert calls == [7]


# ══════════════════════════════════════════════════════════════════════════
# The index pass is the only writer
# ══════════════════════════════════════════════════════════════════════════


def test_no_operator_facing_handler_writes_the_record():
    """The asymmetry is what makes the record mean "what the rows were scanned
    under" rather than "what the assignment is".

    A panel handler that changes `vault_path` must leave these columns alone —
    otherwise the `/old -> unassigned -> /new` transition stamps the *new*
    assignment on rows built under the old one, both facts then agree, the pass
    takes its no-op branch, and the link case that never heals is guaranteed
    rather than merely possible.
    """
    root = Path(__file__).resolve().parent.parent / "src"
    writers = []
    for path in root.rglob("*.py"):
        if path == root / "services" / "indexer.py":
            continue
        text = path.read_text(encoding="utf-8")
        if "indexed_vault_" in text and path != root / "models" / "db.py":
            writers.append(str(path.relative_to(root)))
    assert writers == [], (
        f"{writers} reference the provenance columns; only the index pass and "
        "the model may."
    )


def test_the_discard_log_renders_both_provenances_decoded(tmp_path):
    """The stored realpath is hex, and is decoded **only** for rendering.

    An operator reading a discard line has to be able to see what was replaced;
    a hexadecimal blob in that line would make the one fact they actually read
    unreadable. `decode_realpath` is lossless, so a pathname that cannot be
    spelled in UTF-8 still renders rather than raising.
    """
    described = indexer.describe_recorded(
        ("/vaults/old", indexer.encode_realpath("/data/old"), "1:abcd")
    )
    assert "/vaults/old" in described
    assert "/data/old" in described

    assert indexer.describe_recorded((None, None, None)) == "no record"

    # A non-UTF-8 component renders — escaped by `repr`, which is exactly what
    # a log line wants — rather than raising on the way to the log.
    weird = "/data/" + os.fsdecode(b"\xff")
    rendered = indexer.describe_recorded((weird, indexer.encode_realpath(weird), None))
    assert rendered.count("\\udcff") == 2, rendered
    assert "handle=none" in rendered

    # And a column somebody hand-edited to a non-hex value is reported rather
    # than taking the log line down with it.
    assert "undecodable" in indexer.describe_recorded(("/a", "not-hex", None))


# ══════════════════════════════════════════════════════════════════════════
# A move re-opens the exclusion decision (adversarial round 3)
# ══════════════════════════════════════════════════════════════════════════
#
# The behaviour is a database property and is asserted end to end in
# `tests/integration/test_move_reevaluates_embedding.py`, which runs the real
# `embed_vault` around the real move paths. These two are the fast structural
# guard for the machine that has no PostgreSQL: both statements that change
# `file_path` must also clear `embedded_content_hash`, because the
# certification records that a row's current content has been dealt with and
# the exclusion branch decides *how* by matching the path.


def _assigned_string(tree, name: str) -> str:
    """The concatenated literal parts of `name = ( "..." "..." )`."""
    import ast as _ast

    for node in _ast.walk(tree):
        if not isinstance(node, _ast.Assign):
            continue
        if not any(
            isinstance(t, _ast.Name) and t.id == name for t in node.targets
        ):
            continue
        return "".join(
            part.value
            for part in _ast.walk(node.value)
            if isinstance(part, _ast.Constant) and isinstance(part.value, str)
        )
    raise AssertionError(f"no assignment to {name} found")


def test_the_indexer_move_update_clears_the_embedding_certification():
    """The id-preserving move path updates the row in place, so a stamp
    written under the old path would survive the move."""
    import ast as _ast
    from pathlib import Path as _Path

    tree = _ast.parse(_Path(indexer.__file__).read_text(encoding="utf-8"))
    sql = _assigned_string(tree, "move_upd_sql")
    assert "UPDATE notes_metadata" in sql
    assert "file_path = :new" in sql
    assert "embedded_content_hash = NULL" in sql, (
        "an id-preserving move that keeps embedded_content_hash freezes the "
        "old exclusion decision forever — the embedding pass selects on "
        "`embedded_content_hash != content_hash`, which a preserved stamp "
        "makes false"
    )


def test_move_note_clears_the_embedding_certification():
    """`move_note`'s own metadata UPDATE, the other statement that can carry a
    stamp across a path change."""
    import ast as _ast
    from pathlib import Path as _Path

    import src.mcp_server.tools as tools_module

    tree = _ast.parse(_Path(tools_module.__file__).read_text(encoding="utf-8"))
    values_calls = [
        node
        for node in _ast.walk(tree)
        if isinstance(node, _ast.Call)
        and isinstance(node.func, _ast.Attribute)
        and node.func.attr == "values"
        and any(kw.arg == "file_path" for kw in node.keywords)
    ]
    assert values_calls, "no `.values(file_path=...)` found in tools.py"
    for call in values_calls:
        named = {kw.arg for kw in call.keywords}
        assert "embedded_content_hash" in named, (
            "a statement that changes file_path must also clear "
            f"embedded_content_hash (line {call.lineno})"
        )
        cleared = next(
            kw for kw in call.keywords if kw.arg == "embedded_content_hash"
        )
        assert (
            isinstance(cleared.value, _ast.Constant) and cleared.value.value is None
        ), "it must be set to None — re-evaluate at the next pass"
