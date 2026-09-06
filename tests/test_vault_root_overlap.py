"""Vault-root overlap detection: the two checks, the snapshot, and the gate (#199).

Everything here exercises `src/services/vault_overlap.py` and the refuse-only
consultation `vault._vault_root` makes of it. The filesystem cases use real
directories, real symlinks and real `os.open` — the checks are about inodes and
canonical pathnames, and a mocked `stat` would pin the policy while leaving the
premise unproved.
"""

import asyncio
import contextlib
import datetime
import errno
import os
import threading
from pathlib import Path

import pytest

from src.services import vault, vault_overlap
from src.services.vault_overlap import (
    CAUSE_TIMEOUT,
    CAUSE_UNSTABLE,
    RELATION_CONTAINED_BY,
    RELATION_CONTAINS,
    RELATION_IDENTICAL,
    Overlap,
    QuarantineEntry,
    RootUnexaminable,
    observe_root_blocking,
    relation_between,
)


# ── Helpers ─────────────────────────────────────────────────────────────────


def _observe(path) -> vault_overlap.RootObservation:
    return observe_root_blocking(str(path))


class _FakeSessionFactory:
    """A stand-in for `async_session` returning fixed `(id, username, path)` rows."""

    def __init__(self, rows, *, raises: Exception | None = None):
        self.rows = rows
        self.raises = raises
        self.calls = 0

    def __call__(self):
        return self

    async def __aenter__(self):
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, _statement):
        rows = self.rows

        class _Result:
            def all(self):
                return [
                    type("Row", (), {"id": r[0], "username": r[1], "vault_path": r[2]})()
                    for r in rows
                ]

        return _Result()


def _entry(user_id, username, assignment, reason) -> QuarantineEntry:
    return QuarantineEntry(
        user_id=user_id,
        username=username,
        assignment=assignment,
        reason=reason,
        detected_at=datetime.datetime.now(datetime.timezone.utc),
    )


# ── Check 1: identity ───────────────────────────────────────────────────────


def test_symlink_alias_is_identical(tmp_path):
    """A symlink to another root is a different string and the same inode."""
    real = tmp_path / "team"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real)

    a, b = _observe(real), _observe(alias)
    assert a.assignment != b.assignment
    assert (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)
    assert relation_between(a, b) == RELATION_IDENTICAL
    assert relation_between(b, a) == RELATION_IDENTICAL


def test_second_pathname_for_one_directory_is_identical(tmp_path):
    """Two pathnames reaching one directory through a symlinked *parent*.

    The strings differ in a component that is not the last one, so neither is a
    prefix of the other and only identity can catch it.
    """
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    root = real_parent / "vault"
    root.mkdir()
    link_parent = tmp_path / "linked"
    link_parent.symlink_to(real_parent)

    a, b = _observe(root), _observe(link_parent / "vault")
    assert a.assignment != b.assignment
    assert relation_between(a, b) == RELATION_IDENTICAL


def test_identical_assignment_strings_report_identical(tmp_path):
    """The exact-duplicate case is the degenerate case of the two checks.

    It must report `identical` so the caller can select the wording operators
    already know instead of describing an equal pair as a containment.
    """
    root = tmp_path / "team"
    root.mkdir()
    a, b = _observe(root), _observe(root)
    assert relation_between(a, b) == RELATION_IDENTICAL


def test_identical_strings_report_identical_even_when_unopenable(tmp_path):
    """Two identical assignments collide whether or not either could be opened."""
    missing = str(tmp_path / "gone")
    a, b = _observe(missing), _observe(missing)
    assert not a.examinable
    assert relation_between(a, b) == RELATION_IDENTICAL


def test_distinct_directories_are_not_identical(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    assert not vault_overlap.roots_identical(_observe(a), _observe(b))


# ── Check 2: containment ────────────────────────────────────────────────────


def test_descendant_is_detected(tmp_path):
    outer = tmp_path / "team"
    outer.mkdir()
    inner = outer / "private"
    inner.mkdir()

    a, b = _observe(outer), _observe(inner)
    assert relation_between(a, b) == RELATION_CONTAINS
    assert relation_between(b, a) == RELATION_CONTAINED_BY


def test_nested_symlink_is_detected_through_the_canonical_paths(tmp_path):
    """The assignment strings are siblings; the canonical paths nest."""
    outer = tmp_path / "team"
    outer.mkdir()
    inner = outer / "private"
    inner.mkdir()
    alias = tmp_path / "solo"
    alias.symlink_to(inner)

    a, b = _observe(outer), _observe(alias)
    assert not str(b.assignment).startswith(str(a.assignment))
    assert relation_between(a, b) == RELATION_CONTAINS


def test_siblings_are_accepted(tmp_path):
    a = tmp_path / "alice"
    b = tmp_path / "bob"
    a.mkdir()
    b.mkdir()
    assert relation_between(_observe(a), _observe(b)) is None


def test_string_prefix_sibling_is_accepted(tmp_path):
    """`/vaults/team` is NOT an ancestor of `/vaults/team-2`.

    A raw `startswith` says it is, and would refuse an assignment that overlaps
    nothing — the false-positive direction this codebase treats as the expensive
    failure.
    """
    a = tmp_path / "team"
    b = tmp_path / "team-2"
    a.mkdir()
    b.mkdir()
    assert relation_between(_observe(a), _observe(b)) is None
    assert not vault_overlap.contains_path(str(a), str(b))


@pytest.mark.parametrize(
    "ancestor,descendant,expected",
    [
        ("/vaults/team", "/vaults/team/private", True),
        ("/vaults/team", "/vaults/team-2", False),
        ("/vaults/team", "/vaults/team", False),  # strict: not its own ancestor
        ("/vaults/team", "/vaults/teams/x", False),
        ("/", "/vaults/team", True),
        ("/vaults/team/private", "/vaults/team", False),
    ],
)
def test_contains_path_is_component_wise(ancestor, descendant, expected):
    assert vault_overlap.contains_path(ancestor, descendant) is expected


@pytest.mark.parametrize("spelling", ["{base}/", "{base}//sub", "{base}/."])
def test_the_detector_normalises_every_spelling_of_one_pathname(
    spelling, tmp_path
):
    """`/vaults/team/`, `/vaults//team` and `/vaults/team/.` name one directory.

    Asserted in **both ancestor directions**: the spelling is applied to the
    outer root and then to the inner one, so a normalisation that only worked
    on the left-hand operand of the containment test would still fail here.
    """
    outer = tmp_path / "team"
    outer.mkdir()
    inner = outer / "private"
    inner.mkdir()

    # The repeated-separator form is spelled against the *parent* so the doubled
    # separator lands mid-path, which is where a naive string compare breaks.
    def _spell(path):
        if spelling == "{base}/":
            return f"{path}/"
        if spelling == "{base}//sub":
            return f"{path.parent}//{path.name}"
        return f"{path}/."

    # Direction 1: the ancestor is the oddly spelled one.
    a, b = _observe(_spell(outer)), _observe(inner)
    assert a.assignment == str(outer), "the assignment is stored normalised"
    assert relation_between(a, b) == RELATION_CONTAINS
    assert relation_between(b, a) == RELATION_CONTAINED_BY

    # Direction 2: the descendant is.
    a, b = _observe(outer), _observe(_spell(inner))
    assert b.assignment == str(inner)
    assert relation_between(a, b) == RELATION_CONTAINS
    assert relation_between(b, a) == RELATION_CONTAINED_BY

    # And one directory spelled two ways is `identical`, not a containment.
    assert relation_between(_observe(outer), _observe(_spell(outer))) == (
        RELATION_IDENTICAL
    )


@pytest.mark.parametrize("spelling", ["{base}/", "{base}//sub", "{base}/."])
async def test_detection_names_both_users_however_the_root_is_spelled(
    detecting, spelling, tmp_path
):
    """The same three spellings, through `detect_and_publish` rather than the
    predicate — the assignment reaches the detector as a `users.vault_path`
    string, and that is the string an administrator typed."""
    outer = tmp_path / "team"
    outer.mkdir()
    inner = outer / "private"
    inner.mkdir()

    def _spell(path):
        if spelling == "{base}/":
            return f"{path}/"
        if spelling == "{base}//sub":
            return f"{path.parent}//{path.name}"
        return f"{path}/."

    for rows in (
        [(1, "alice", _spell(outer)), (2, "bob", str(inner))],
        [(1, "alice", str(outer)), (2, "bob", _spell(inner))],
    ):
        vault_overlap.reset_snapshot_state()
        snapshot = await vault_overlap.detect_and_publish(_FakeSessionFactory(rows))
        assert set(snapshot.entries) == {1, 2}, rows
        # The recorded facts are the normalised forms, so the operator surfaces
        # never render a doubled separator back at the person who typed it.
        assert snapshot.entries[1].assignment == str(outer)
        assert snapshot.entries[2].assignment == str(inner)
        assert snapshot.entries[1].reason.relation == RELATION_CONTAINS
        assert snapshot.entries[2].reason.relation == RELATION_CONTAINED_BY


def test_a_path_does_not_contain_itself(tmp_path):
    root = tmp_path / "team"
    root.mkdir()
    assert not vault_overlap.contains_path(str(root), str(root))


# ── Observation: verdicts, descriptors, deadline ────────────────────────────


def test_missing_root_is_a_verdict_not_an_exception(tmp_path):
    observation = _observe(tmp_path / "not-there")
    assert not observation.examinable
    assert observation.cause == errno.ENOENT
    assert observation.st_dev is None and observation.realpath is None


def test_a_file_is_not_a_directory_root(tmp_path):
    target = tmp_path / "note.md"
    target.write_text("x")
    observation = _observe(target)
    assert observation.cause == errno.ENOTDIR


def test_observation_closes_its_descriptor(tmp_path):
    """No descriptor survives an observation, on the success or the failure path."""
    root = tmp_path / "team"
    root.mkdir()
    before = len(os.listdir("/proc/self/fd"))
    for _ in range(50):
        _observe(root)
        _observe(tmp_path / "missing")
        _observe(tmp_path)
    after = len(os.listdir("/proc/self/fd"))
    assert after <= before + 1


def test_unexaminable_root_names_no_peer(tmp_path):
    reason = RootUnexaminable(errno.ENOENT)
    assert not isinstance(reason, Overlap)
    assert not hasattr(reason, "peer_user_id")
    text = vault_overlap.operator_text(
        _entry(7, "carol", str(tmp_path / "gone"), reason)
    )
    assert "ENOENT" in text
    assert "no peer was observed" in text.lower()


async def test_observation_deadline_is_a_timeout_verdict(monkeypatch, tmp_path):
    """A blocking observation past the deadline yields `root_unexaminable(timeout)`.

    Asserted with a fake blocking observation, not a real slow filesystem.
    """
    def _block(assignment, *, user_id=None, username=None):
        import time

        time.sleep(5)
        raise AssertionError("unreachable")

    monkeypatch.setattr(vault_overlap, "observe_root_blocking", _block)
    observation = await vault_overlap.observe_root(
        str(tmp_path), user_id=1, username="alice", timeout=0.05
    )
    assert observation.cause == CAUSE_TIMEOUT
    assert observation.user_id == 1
    assert not observation.examinable


async def test_a_timed_out_root_is_one_users_verdict(detecting, monkeypatch, tmp_path):
    """The detection still publishes, and every other root is still observed."""
    slow = tmp_path / "slow"
    slow.mkdir()
    a = tmp_path / "alice"
    a.mkdir()
    b = tmp_path / "bob"
    b.mkdir()

    real = vault_overlap.observe_root_blocking

    def _maybe_block(assignment, *, user_id=None, username=None):
        if assignment == str(slow):
            import time

            time.sleep(5)
        return real(assignment, user_id=user_id, username=username)

    monkeypatch.setattr(vault_overlap, "observe_root_blocking", _maybe_block)
    monkeypatch.setattr(
        vault_overlap.settings, "vault_root_observe_timeout_seconds", 0.05
    )

    factory = _FakeSessionFactory(
        [(1, "hung", str(slow)), (2, "alice", str(a)), (3, "bob", str(b))]
    )
    snapshot = await vault_overlap.detect_and_publish(factory)

    assert set(snapshot.entries) == {1}
    assert snapshot.entries[1].reason == RootUnexaminable(CAUSE_TIMEOUT)
    assert vault_overlap.published_snapshot() is snapshot


async def test_an_unstable_realpath_is_unexaminable_not_an_overlap(
    monkeypatch, tmp_path
):
    """A pathname moving under the check is a "could not look", never an overlap."""
    root = tmp_path / "team"
    root.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setattr(os.path, "realpath", lambda p: str(other))

    observation = _observe(root)
    assert observation.cause == CAUSE_UNSTABLE


# ── Detection and the snapshot ──────────────────────────────────────────────


@pytest.fixture
def detecting(monkeypatch):
    """Multi-user mode, which is the only mode a detection runs in at all.

    `_detect` answers an empty snapshot **before** it touches the database or
    the filesystem when `multi_user_mode` is off, so every case below that
    hands it a non-empty user list has to say which mode it is describing.
    The single-user behaviour is asserted on its own, further down, with a
    populated fake session that must go unread.
    """
    monkeypatch.setattr(vault_overlap.settings, "multi_user_mode", True)




async def test_detection_publishes_the_overlapping_pair(detecting, tmp_path):
    outer = tmp_path / "team"
    outer.mkdir()
    inner = outer / "private"
    inner.mkdir()
    unrelated = tmp_path / "solo"
    unrelated.mkdir()

    factory = _FakeSessionFactory(
        [(1, "alice", str(outer)), (2, "bob", str(inner)), (3, "carol", str(unrelated))]
    )
    snapshot = await vault_overlap.detect_and_publish(factory)

    assert set(snapshot.entries) == {1, 2}
    assert snapshot.entries[1].reason == Overlap(2, "bob", str(inner), RELATION_CONTAINS)
    assert snapshot.entries[2].reason == Overlap(
        1, "alice", str(outer), RELATION_CONTAINED_BY
    )
    assert snapshot.reason_for(3) is None
    assert not snapshot.names(3)


async def test_detection_records_the_facts_as_observed(detecting, tmp_path):
    """The entry stays nameable after the peer row changes — nothing is re-read."""
    outer = tmp_path / "team"
    outer.mkdir()
    inner = outer / "private"
    inner.mkdir()

    factory = _FakeSessionFactory([(1, "alice", str(outer)), (2, "bob", str(inner))])
    snapshot = await vault_overlap.detect_and_publish(factory)

    entry = snapshot.entries[1]
    assert entry.username == "alice"
    assert entry.assignment == str(outer)
    assert entry.reason.peer_username == "bob"
    assert entry.reason.peer_assignment == str(inner)
    assert entry.detected_at.tzinfo is not None

    # The peer disappears from the database entirely; the snapshot is unmoved.
    factory.rows = []
    assert snapshot.entries[1].reason.peer_username == "bob"
    text = vault_overlap.operator_text(snapshot.entries[1])
    assert "alice" in text and "bob" in text


async def test_no_active_assignment_publishes_an_empty_snapshot(detecting):
    """No active user holds an assignment: nothing to compare, nothing named."""
    snapshot = await vault_overlap.detect_and_publish(_FakeSessionFactory([]))
    assert snapshot.entries == {}
    assert vault_overlap.is_published()


async def test_single_user_mode_answers_before_it_touches_anything(
    monkeypatch, tmp_path
):
    """`multi_user_mode` off short-circuits **before** the query and the opens.

    The check used to sit after `_active_assignments`, so a single-user
    deployment whose `users` table still held rows — a flag flipped back, or a
    configuration that was multi-user once — issued that query and then opened
    every root in it, once per pass entry point, to build a snapshot nothing in
    that mode reads: `_vault_root(None)` never consults it and
    `_refuse_quarantined_pass` returns immediately for a null user.

    The fake session is deliberately **not empty**, so a detection that reached
    the enumeration would find two overlapping roots and publish them. It must
    find nothing, because it must not look.
    """
    outer = tmp_path / "team"
    outer.mkdir()
    inner = outer / "private"
    inner.mkdir()

    monkeypatch.setattr(vault_overlap.settings, "multi_user_mode", False)

    def _boom(*_a, **_k):
        raise AssertionError("single-user detection touched the filesystem")

    monkeypatch.setattr(vault_overlap, "observe_root_blocking", _boom)
    monkeypatch.setattr(vault_overlap, "observe_root_blocking_retaining", _boom)

    factory = _FakeSessionFactory(
        [(1, "alice", str(outer)), (2, "bob", str(inner))]
    )
    snapshot = await vault_overlap.detect_and_publish(factory)

    assert snapshot.entries == {}, (
        "single-user mode enumerated the users table and observed its roots"
    )
    assert factory.calls == 0, "the enumeration query was issued"
    assert vault_overlap.is_published(), (
        "the snapshot must still be published — the readiness state is a "
        "refusal, and single-user mode must never sit in it"
    )


async def test_sandbox_mode_publishes_without_touching_the_filesystem(monkeypatch):
    monkeypatch.setattr(vault_overlap.settings, "mcp_sandbox_mode", True)

    def _never(*a, **k):  # pragma: no cover - asserted by not being called
        raise AssertionError("sandbox mode must open no root")

    monkeypatch.setattr(vault_overlap, "observe_root_blocking", _never)
    factory = _FakeSessionFactory([(1, "alice", "/vaults/a")])
    snapshot = await vault_overlap.detect_and_publish(factory)

    assert snapshot.entries == {}
    assert factory.calls == 0


async def test_unexaminable_root_quarantines_only_its_own_user(detecting, tmp_path):
    a = tmp_path / "alice"
    a.mkdir()
    b = tmp_path / "bob"
    b.mkdir()
    factory = _FakeSessionFactory(
        [
            (1, "gone", str(tmp_path / "missing")),
            (2, "alice", str(a)),
            (3, "bob", str(b)),
        ]
    )
    snapshot = await vault_overlap.detect_and_publish(factory)

    assert set(snapshot.entries) == {1}
    assert snapshot.entries[1].reason == RootUnexaminable(errno.ENOENT)


async def test_a_corrected_condition_clears_at_the_next_detection(detecting, tmp_path):
    outer = tmp_path / "team"
    outer.mkdir()
    inner = outer / "private"
    inner.mkdir()
    elsewhere = tmp_path / "solo"
    elsewhere.mkdir()

    factory = _FakeSessionFactory([(1, "alice", str(outer)), (2, "bob", str(inner))])
    first = await vault_overlap.detect_and_publish(factory)
    assert set(first.entries) == {1, 2}

    factory.rows = [(1, "alice", str(outer)), (2, "bob", str(elsewhere))]
    second = await vault_overlap.detect_and_publish(factory)
    assert second.entries == {}
    assert vault_overlap.published_snapshot() is second


async def test_an_alias_created_after_assignment_is_detected(detecting, tmp_path):
    """Both assignments are unchanged strings; one becomes a link to the other."""
    a = tmp_path / "alice"
    a.mkdir()
    b = tmp_path / "bob"
    b.mkdir()
    factory = _FakeSessionFactory([(1, "alice", str(a)), (2, "bob", str(b))])
    assert (await vault_overlap.detect_and_publish(factory)).entries == {}

    b.rmdir()
    b.symlink_to(a)
    second = await vault_overlap.detect_and_publish(factory)
    assert set(second.entries) == {1, 2}
    assert second.entries[1].reason.relation == RELATION_IDENTICAL


async def test_a_failed_redetection_retains_the_previous_snapshot(detecting, tmp_path, caplog):
    outer = tmp_path / "team"
    outer.mkdir()
    inner = outer / "private"
    inner.mkdir()
    good = _FakeSessionFactory([(1, "alice", str(outer)), (2, "bob", str(inner))])
    first = await vault_overlap.detect_and_publish(good)

    broken = _FakeSessionFactory([], raises=RuntimeError("database is away"))
    with caplog.at_level("ERROR"):
        retained = await vault_overlap.detect_and_publish(broken)

    assert retained is first
    assert vault_overlap.published_snapshot() is first
    assert set(vault_overlap.published_snapshot().entries) == {1, 2}
    assert any(record.levelname == "ERROR" for record in caplog.records)


async def test_a_failed_first_detection_stays_never_published(
    detecting, unpublished_vault_root_snapshot, caplog
):
    broken = _FakeSessionFactory([], raises=RuntimeError("database is away"))
    with caplog.at_level("ERROR"):
        with pytest.raises(RuntimeError):
            await vault_overlap.detect_and_publish(broken)

    assert vault_overlap.published_snapshot() is None
    assert not vault_overlap.is_published()
    assert any(record.levelname == "ERROR" for record in caplog.records)


async def test_the_snapshot_mapping_is_immutable(detecting, tmp_path):
    root = tmp_path / "solo"
    root.mkdir()
    snapshot = await vault_overlap.detect_and_publish(
        _FakeSessionFactory([(1, "alice", str(root))])
    )
    with pytest.raises(TypeError):
        snapshot.entries[9] = None


# ── Serialization and monotonic publication ─────────────────────────────────


async def test_detections_do_not_interleave(detecting, tmp_path):
    """The second detection does not begin observing until the first published."""
    root = tmp_path / "solo"
    root.mkdir()
    order: list[str] = []
    real = vault_overlap.observe_root

    async def _tracked(assignment, **kwargs):
        order.append(f"observe:{kwargs.get('username')}")
        await asyncio.sleep(0.02)
        return await real(assignment, **kwargs)

    original_publish = vault_overlap.publish

    def _tracked_publish(snapshot):
        order.append(f"publish:{snapshot.sequence}")
        return original_publish(snapshot)

    vault_overlap.observe_root = _tracked
    vault_overlap.publish = _tracked_publish
    try:
        factory_a = _FakeSessionFactory([(1, "alice", str(root))])
        factory_b = _FakeSessionFactory([(2, "bob", str(root))])
        await asyncio.gather(
            vault_overlap.detect_and_publish(factory_a),
            vault_overlap.detect_and_publish(factory_b),
        )
    finally:
        vault_overlap.observe_root = real
        vault_overlap.publish = original_publish

    # Whatever the interleaving of the two tasks, no observation of the second
    # detection may appear before the first detection's publication.
    assert order[1].startswith("publish:")
    assert len(order) == 4


async def test_a_stalled_older_detection_does_not_overwrite_a_newer_quarantine(
    detecting, tmp_path,
):
    """The lock, end to end: the older result must not re-admit both tenants.

    The older detection is delayed *inside* its root observation and completes
    last. Under a lock scoped to the publication alone it would publish its own
    empty result over the newer quarantine.
    """
    outer = tmp_path / "team"
    outer.mkdir()
    inner = outer / "private"
    inner.mkdir()
    solo = tmp_path / "solo"
    solo.mkdir()

    real = vault_overlap.observe_root
    gate = asyncio.Event()

    async def _delayed(assignment, **kwargs):
        if kwargs.get("username") == "slow":
            await gate.wait()
        return await real(assignment, **kwargs)

    vault_overlap.observe_root = _delayed
    try:
        stale = asyncio.create_task(
            vault_overlap.detect_and_publish(
                _FakeSessionFactory([(9, "slow", str(solo))])
            )
        )
        await asyncio.sleep(0)  # let the stale detection take the lock
        newer = asyncio.create_task(
            vault_overlap.detect_and_publish(
                _FakeSessionFactory(
                    [(1, "alice", str(outer)), (2, "bob", str(inner))]
                )
            )
        )
        await asyncio.sleep(0.05)
        gate.set()
        await asyncio.gather(stale, newer)
    finally:
        vault_overlap.observe_root = real

    published = vault_overlap.published_snapshot()
    assert set(published.entries) == {1, 2}


async def test_an_out_of_order_publication_is_discarded(detecting, tmp_path):
    """The sequence guard, asserted without the lock.

    A future caller — a test, a fixture, an entry point added later — that
    publishes outside the critical section must still not move the snapshot
    backwards. The lock is the mechanism; this is the invariant.
    """
    outer = tmp_path / "team"
    outer.mkdir()
    inner = outer / "private"
    inner.mkdir()
    current = await vault_overlap.detect_and_publish(
        _FakeSessionFactory([(1, "alice", str(outer)), (2, "bob", str(inner))])
    )

    older = vault_overlap.QuarantineSnapshot(
        sequence=current.sequence - 1,
        detected_at=datetime.datetime.now(datetime.timezone.utc),
    )
    assert vault_overlap.publish(older) is False
    assert vault_overlap.published_snapshot() is current

    same = vault_overlap.QuarantineSnapshot(
        sequence=current.sequence,
        detected_at=datetime.datetime.now(datetime.timezone.utc),
    )
    assert vault_overlap.publish(same) is False
    assert vault_overlap.published_snapshot() is current

    newer = vault_overlap.QuarantineSnapshot(
        sequence=current.sequence + 1,
        detected_at=datetime.datetime.now(datetime.timezone.utc),
    )
    assert vault_overlap.publish(newer) is True
    assert vault_overlap.published_snapshot() is newer


# ── The gate ────────────────────────────────────────────────────────────────


@pytest.fixture
def multi_user(monkeypatch):
    monkeypatch.setattr(vault.settings, "multi_user_mode", True)
    vault.clear_user_vault_cache()
    yield
    vault.clear_user_vault_cache()


def test_gate_admits_a_caller_the_snapshot_does_not_name(multi_user, tmp_path):
    vault._user_vault_cache[5] = Path(tmp_path)
    vault_overlap.publish_synthetic_snapshot(
        [_entry(6, "bob", "/vaults/bob", RootUnexaminable(errno.ENOENT))]
    )
    assert vault._vault_root(5) == Path(tmp_path)


def test_gate_refuses_an_overlapping_caller(multi_user, tmp_path):
    vault._user_vault_cache[5] = Path(tmp_path)
    vault_overlap.publish_synthetic_snapshot(
        [
            _entry(
                5,
                "alice",
                "/vaults/team",
                Overlap(6, "bob", "/vaults/team/private", RELATION_CONTAINS),
            )
        ]
    )
    with pytest.raises(vault.VaultRootOverlap) as excinfo:
        vault._vault_root(5)
    assert isinstance(excinfo.value, RuntimeError)


def test_gate_refuses_an_unexaminable_caller(multi_user, tmp_path):
    vault._user_vault_cache[5] = Path(tmp_path)
    vault_overlap.publish_synthetic_snapshot(
        [_entry(5, "alice", "/vaults/team", RootUnexaminable(errno.ENOENT))]
    )
    with pytest.raises(vault.VaultRootUnexaminable) as excinfo:
        vault._vault_root(5)
    assert isinstance(excinfo.value, RuntimeError)


def test_gate_refuses_before_the_first_snapshot(
    multi_user, unpublished_vault_root_snapshot, tmp_path
):
    vault._user_vault_cache[5] = Path(tmp_path)
    with pytest.raises(vault.VaultRootNotReady) as excinfo:
        vault._vault_root(5)
    assert isinstance(excinfo.value, RuntimeError)


def test_the_three_refusals_are_distinct_types():
    for kind in (
        vault.VaultRootOverlap,
        vault.VaultRootUnexaminable,
        vault.VaultRootNotReady,
    ):
        assert issubclass(kind, RuntimeError)
    assert not issubclass(vault.VaultRootOverlap, vault.VaultRootUnexaminable)
    assert not issubclass(vault.VaultRootNotReady, vault.VaultRootOverlap)
    assert not issubclass(vault.VaultRootUnexaminable, vault.VaultRootNotReady)


def test_refusal_messages_name_no_other_tenant():
    for message in (
        vault.VAULT_ROOT_OVERLAP_ERROR,
        vault.VAULT_ROOT_UNEXAMINABLE_ERROR,
        vault.VAULT_ROOT_NOT_READY_ERROR,
    ):
        lowered = message.lower()
        assert "bob" not in lowered
        assert "/vaults" not in lowered
        assert ".md" not in lowered
        assert "user_id" not in lowered


def test_readiness_is_never_consulted_for_a_null_user(
    monkeypatch, unpublished_vault_root_snapshot
):
    """Single-user mode never reaches the snapshot, including the ready state."""
    monkeypatch.setattr(vault.settings, "multi_user_mode", False)

    def _never():  # pragma: no cover - asserted by not being called
        raise AssertionError("the gate consulted the snapshot for user_id=None")

    monkeypatch.setattr(vault_overlap, "published_snapshot", _never)
    assert vault._vault_root(None) == Path(vault.settings.vault_path)


def test_the_gate_opens_no_session_and_no_descriptor(multi_user, monkeypatch, tmp_path):
    """The quarantine test is a mapping lookup: no query, no syscall."""
    vault._user_vault_cache[5] = Path(tmp_path)
    vault_overlap.publish_synthetic_snapshot(
        [_entry(5, "alice", "/vaults/team", RootUnexaminable(errno.ENOENT))]
    )

    def _never_open(*a, **k):  # pragma: no cover - asserted by not being called
        raise AssertionError("the gate made a filesystem call")

    monkeypatch.setattr(os, "open", _never_open)
    monkeypatch.setattr(os.path, "realpath", _never_open)
    with pytest.raises(vault.VaultRootUnexaminable):
        vault._vault_root(5)


def test_the_quarantine_test_cannot_admit(multi_user):
    """A caller with no assignment and no quarantine is still refused."""
    vault_overlap.publish_synthetic_snapshot()
    with pytest.raises(RuntimeError) as excinfo:
        vault._vault_root(4242)
    assert not isinstance(excinfo.value, vault.VaultRootOverlap)
    assert not isinstance(excinfo.value, vault.VaultRootNotReady)


# ── Wordings ────────────────────────────────────────────────────────────────


def test_the_exact_duplicate_wording_is_preserved():
    """Task 2.3's requirement, asserted where the wording lives."""
    assert vault_overlap.assignment_conflict_message(
        "/vaults/team", RELATION_IDENTICAL, "bob"
    ) == "Vault path '/vaults/team' is already assigned to user 'bob'."


@pytest.mark.parametrize(
    "relation,fragment",
    [
        (RELATION_CONTAINS, "contains the vault of user 'bob'"),
        (RELATION_CONTAINED_BY, "is inside the vault of user 'bob'"),
    ],
)
def test_the_relation_wordings_name_the_conflicting_user(relation, fragment):
    message = vault_overlap.assignment_conflict_message("/vaults/team", relation, "bob")
    assert fragment in message
    assert "/vaults/team" in message


def test_the_unexaminable_peer_wording_reports_no_overlap():
    message = vault_overlap.peer_unexaminable_message("/vaults/bob", errno.ENOENT)
    assert "could not be ruled out" in message
    assert "overlaps" not in message


def test_cause_text_distinguishes_a_timeout_from_an_errno():
    assert "exceeded" in vault_overlap.cause_text(CAUSE_TIMEOUT)
    assert "ENOENT" in vault_overlap.cause_text(errno.ENOENT)
    assert vault_overlap.cause_text(CAUSE_TIMEOUT) != vault_overlap.cause_text(
        errno.ENOENT
    )


# ── The assignment validator's filesystem check is bounded and off-loop ──────


async def test_the_validator_does_not_block_the_event_loop_on_a_hung_root(
    monkeypatch, tmp_path
):
    """`validate_vault_root_path`'s existence check runs off the loop, under
    the observation deadline (#199, adversarial round 1).

    It used to be `Path(normalized).is_dir()` called inline from
    `edit_user_submit` — a synchronous `stat` against a bind mount, on the event
    loop, **after** the handler had taken the transaction-scoped account guard.
    A network- or FUSE-backed root that stops answering therefore stalled every
    request the process was serving, from inside the one page an operator would
    use to reassign the vault away from that mount, and held the guard while it
    did.

    The stub blocks for far longer than the deadline. What is asserted is that
    the call returns inside it, that the loop kept running while it did, and
    that the verdict is a refusal naming the cause rather than "does not exist"
    — an administrator sent to look for a directory that is present would lose
    the incident to the wrong hypothesis.
    """
    root = tmp_path / "team"
    root.mkdir()

    started = asyncio.Event()
    release = threading.Event()

    def _blocking_observe(assignment, **kwargs):
        started.set()
        release.wait(30)
        raise AssertionError("the deadline did not abandon the wait")

    monkeypatch.setattr(vault_overlap, "observe_root_blocking", _blocking_observe)
    monkeypatch.setattr(
        vault_overlap.settings, "vault_root_observe_timeout_seconds", 0.05
    )
    monkeypatch.setattr(vault.settings, "vault_path", str(root))

    ticks = 0

    async def _tick():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.005)
            ticks += 1

    ticker = asyncio.create_task(_tick())
    try:
        normalized, error = await asyncio.wait_for(
            vault.validate_vault_root_path(str(root)), 5
        )
    finally:
        ticker.cancel()
        release.set()
        with contextlib.suppress(asyncio.CancelledError):
            await ticker

    assert normalized is None
    assert "could not be examined" in error
    assert "does not exist" not in error
    assert ticks > 0, "the event loop was blocked for the whole observation"


async def test_the_validator_still_refuses_a_missing_root_with_the_known_wording(
    monkeypatch, tmp_path
):
    """The message operators know is unchanged. It names the likeliest cause,
    and a mount configured but not applied is exactly that cause."""
    missing = tmp_path / "not-there"
    # Admitted lexically (it is the configured legacy mount) so the case under
    # test is the *filesystem* verdict and not the `/vaults/` rule.
    monkeypatch.setattr(vault.settings, "vault_path", str(missing))
    normalized, error = await vault.validate_vault_root_path(str(missing))
    assert normalized is None
    assert "does not exist as a directory" in error
    assert "docker-compose volume mount" in error


async def test_the_validator_admits_a_real_directory(monkeypatch, tmp_path):
    root = tmp_path / "team"
    root.mkdir()
    monkeypatch.setattr(vault.settings, "vault_path", str(root))
    assert await vault.validate_vault_root_path(str(root)) == (str(root), None)


async def test_a_file_is_refused_by_the_kernel_not_by_a_stat_race(
    monkeypatch, tmp_path
):
    """`O_DIRECTORY` is what refuses it, so there is no window between the
    check and the open in which the path could become a directory."""
    target = tmp_path / "team"
    target.write_text("not a directory")
    monkeypatch.setattr(vault.settings, "vault_path", str(target))
    normalized, error = await vault.validate_vault_root_path(str(target))
    assert normalized is None
    assert "does not exist as a directory" in error


def test_the_lexical_half_touches_no_filesystem(monkeypatch):
    """Split apart so the shape of a string is decided without a syscall.

    Every way of reaching the filesystem raises; the lexical validator must
    still answer, because the rules it enforces are about the string.
    """
    def _boom(*_a, **_k):
        raise AssertionError("the lexical validator touched the filesystem")

    for target in ("open", "stat", "scandir", "listdir"):
        monkeypatch.setattr(os, target, _boom)
    monkeypatch.setattr(os.path, "realpath", _boom)

    assert vault.validate_vault_root_lexical("") == (None, None)
    assert vault.validate_vault_root_lexical("/vaults/team") == (
        "/vaults/team", None
    )
    assert vault.validate_vault_root_lexical("/vaults/team/") == (
        "/vaults/team", None
    )
    assert vault.validate_vault_root_lexical("/etc")[1] is not None
    assert "traversal" in vault.validate_vault_root_lexical("/vaults/../etc")[1]
