"""Regression test for GitHub issue #13.

The indexer's re-resolution pass in `_update_links_for_changed` patches
previously-dangling `note_links` rows when a note (re)appears. The buggy
version matched `target_path IN (:full, :stem, :no_ext)` unconditionally,
where `:stem` is the bare filename without extension.

When two notes share a stem (e.g. `a/Foo.md` and `b/Foo.md`), reindexing
one of them would attach *every* dangling `[[Foo]]` row to it — including
rows that the resolver (`resolve_target`, same-folder + alphabetical
tie-break) would assign to the other note. The fix: only fold the bare
stem into the IN clause when that stem maps to exactly one note.

This test runs fully offline. It drives `_update_links_for_changed` with a
fake async session that records the re-resolution UPDATE statements and a
tmp_path vault holding the real markdown files the function reads.
"""
import asyncio
import re

import pytest

from src.services.indexer import _update_links_for_changed


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Row:
    def __init__(self, file_path, id):
        self.file_path = file_path
        self.id = id


class _FakeSession:
    """Minimal async session double.

    - The first `execute` (the SELECT that builds the vault_index) returns
      the configured notes_metadata rows.
    - delete/insert statements (SQLAlchemy Core constructs) are ignored.
    - `text(...)` UPDATE statements are recorded with their params for
      assertions.
    """

    def __init__(self, rows):
        self._rows = rows
        self._select_served = False
        self.updates = []  # list of (sql_text, params)

    async def execute(self, statement, params=None):
        # The text() UPDATE statements carry a `.text` attribute.
        sql = getattr(statement, "text", None)
        if isinstance(sql, str) and sql.lstrip().upper().startswith("UPDATE"):
            self.updates.append((sql, params or {}))
            return _FakeResult([])
        # First non-text execute is the vault_index SELECT.
        if not self._select_served:
            self._select_served = True
            return _FakeResult(self._rows)
        return _FakeResult([])

    async def commit(self):
        pass


def _update_targets(session):
    """Return the set of target_path values folded into the IN clause across
    all recorded UPDATE statements."""
    folded = set()
    for sql, params in session.updates:
        in_match = re.search(r"target_path IN \(([^)]*)\)", sql)
        assert in_match, f"no IN clause found in: {sql}"
        bind_names = re.findall(r":(\w+)", in_match.group(1))
        for name in bind_names:
            folded.add(params[name])
    return folded


def test_shared_stem_does_not_fold_bare_stem(tmp_path):
    """Reindexing one of two notes that share a stem must NOT match the bare
    stem in the re-resolution UPDATE."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "a" / "Foo.md").write_text("a body", encoding="utf-8")
    (tmp_path / "b" / "Foo.md").write_text("b body", encoding="utf-8")

    rows = [_Row("a/Foo.md", 1), _Row("b/Foo.md", 2)]
    session = _FakeSession(rows)

    asyncio.run(
        _update_links_for_changed(
            session, tmp_path, ["a/Foo.md"], user_id=None
        )
    )

    folded = _update_targets(session)
    # The full path and the no-extension path are always safe.
    assert "a/Foo.md" in folded
    assert "a/Foo" in folded
    # The bare ambiguous stem must NOT be folded in — that is the bug.
    assert "Foo" not in folded


def test_unique_stem_folds_bare_stem(tmp_path):
    """When a stem is unique in the vault, the bare stem IS safe to match."""
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "Bar.md").write_text("bar body", encoding="utf-8")

    rows = [_Row("a/Bar.md", 1)]
    session = _FakeSession(rows)

    asyncio.run(
        _update_links_for_changed(
            session, tmp_path, ["a/Bar.md"], user_id=None
        )
    )

    folded = _update_targets(session)
    assert "a/Bar.md" in folded
    assert "a/Bar" in folded
    # Unique stem → safe to fold the bare form.
    assert "Bar" in folded


def test_multi_user_scope_clause_preserved(tmp_path):
    """The per-user source-note scope guard survives the rewrite."""
    (tmp_path / "Baz.md").write_text("baz", encoding="utf-8")

    rows = [_Row("Baz.md", 7)]
    session = _FakeSession(rows)

    asyncio.run(
        _update_links_for_changed(
            session, tmp_path, ["Baz.md"], user_id=42
        )
    )

    assert session.updates, "expected at least one UPDATE"
    for sql, params in session.updates:
        assert "source_note_id IN" in sql
        assert "WHERE user_id = :uid" in sql
        assert params["uid"] == 42
