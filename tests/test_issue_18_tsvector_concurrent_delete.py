"""Regression test for GitHub issue #18.

`index_vault` scans the vault once to build `to_upsert`, commits the metadata
rows, then runs a SECOND loop that updates each changed note's
`content_tsvector`. Originally that second loop re-read every file from disk.
If a file was deleted on disk between the two passes (a concurrent delete —
e.g. the user removing a note in Obsidian, or a `move_note`), the re-read
raised `FileNotFoundError`, which the loop swallowed. The metadata row was
already committed but its `content_tsvector` was never set, leaving the note
unsearchable via full-text search until a later reindex happened to catch it.

The fix carries the body parsed during the first scan loop (`path_to_content`)
into the tsvector loop, so disk is read exactly once. A file vanishing after
the scan no longer prevents its tsvector from being written.

This test drives the real `index_vault` against a temp vault on disk, using a
fake `async_session` that deletes the file from disk right after the metadata
upsert (simulating the concurrent delete). It then asserts the tsvector UPDATE
still ran with the in-memory body.

Fully offline: no DB, no network, no embedding provider. The session is faked;
`embed_vault`/links are never reached because the fake records only what
`index_vault` itself executes.
"""

import os
import tempfile

import pytest

# Avoid loading the host's real `./.env` (forbidden host keys): set the same
# minimal defaults conftest uses and chdir away from any `.env` BEFORE import.
os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")
os.chdir(tempfile.gettempdir())

from sqlalchemy.dialects.postgresql import Insert  # noqa: E402
from sqlalchemy.sql.elements import TextClause  # noqa: E402

import src.services.indexer as indexer  # noqa: E402


class _FakeResult:
    """Mimics the slice of the SQLAlchemy Result API that index_vault uses."""

    def __init__(self, rows=None):
        self._rows = rows or []

    def fetchall(self):
        return self._rows

    def all(self):
        return self._rows

    def scalar(self):
        return 0

    def scalar_one(self):
        raise AssertionError("scalar_one not expected in this test")


class _FakeSession:
    """Records executed statements and triggers a concurrent delete.

    - The existing-hash SELECT returns no rows, so the single vault file is
      treated as new and lands in `to_upsert`.
    - When the metadata INSERT (the upsert) executes, we delete the file from
      disk — reproducing the issue #18 race between the scan loop and the
      tsvector loop.
    - The `content_tsvector` UPDATE (a TextClause) is captured for assertions.
    """

    def __init__(self, on_metadata_insert):
        self._on_metadata_insert = on_metadata_insert
        self.tsvector_params = []
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def begin_nested(self):
        """The savepoint each keyword-vector attempt runs in (#127, D4).

        A no-op here: this stub records statements rather than executing them,
        so there is nothing to roll back. What a stub can never show is that
        the driver's aborted-transaction state clears between attempts — that
        is why the retreat has a real-PostgreSQL test
        (`tests/integration/test_tsvector_bounded_pg.py`).
        """
        class _Savepoint:
            async def __aenter__(self_inner):
                return self_inner

            async def __aexit__(self_inner, *exc):
                return False

        return _Savepoint()

    async def execute(self, stmt, params=None):
        # Capture the tsvector UPDATE (the thing the bug skipped on delete).
        if isinstance(stmt, TextClause) and "content_tsvector" in stmt.text:
            self.tsvector_params.append(params)
            return _FakeResult()

        # Simulate the concurrent on-disk delete the instant the row is
        # committed-ready (mirrors the real "row exists, file gone" window).
        if isinstance(stmt, Insert) and stmt.table.name == "notes_metadata":
            self._on_metadata_insert()
            return _FakeResult()

        # Everything else (existing-hash SELECT, link/select/delete) is inert.
        return _FakeResult()

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        return None


@pytest.mark.asyncio
async def test_tsvector_written_despite_concurrent_delete(monkeypatch, tmp_path):
    """A file deleted after the scan loop must still get its tsvector set."""
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "racey.md"
    body = "The quick brown fox jumps over the lazy dog."
    note.write_text(f"---\ntitle: Racey\n---\n{body}\n", encoding="utf-8")

    # Point single-user mode at our temp vault.
    monkeypatch.setattr(indexer.settings, "vault_path", str(vault), raising=False)

    def _delete_file():
        # The concurrent delete: gone before the tsvector loop runs.
        if note.exists():
            note.unlink()

    fake = _FakeSession(on_metadata_insert=_delete_file)
    monkeypatch.setattr(indexer, "async_session", lambda: fake)

    await indexer.index_vault()

    # The file was deleted mid-pass, yet the tsvector UPDATE must have run for
    # it, carrying the body parsed during the scan loop (not a disk re-read).
    assert fake.tsvector_params, (
        "content_tsvector was never updated — the concurrent delete suppressed "
        "the tsvector write (issue #18 regression)"
    )
    matching = [
        p for p in fake.tsvector_params
        if p and p.get("path") == "racey.md"
    ]
    assert matching, "no tsvector UPDATE was issued for the deleted note"
    assert body in matching[0]["content"], (
        "tsvector content did not come from the in-memory scan body"
    )
    assert fake.commits == 1, "the index snapshot must commit exactly once"


@pytest.mark.asyncio
async def test_link_failure_does_not_commit_new_metadata_hash(monkeypatch, tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "note.md"
    note.write_text("body", encoding="utf-8")
    monkeypatch.setattr(indexer.settings, "vault_path", str(vault), raising=False)

    fake = _FakeSession(on_metadata_insert=lambda: None)
    monkeypatch.setattr(indexer, "async_session", lambda: fake)

    async def fail_links(*_args, **_kwargs):
        raise RuntimeError("link insert failed")

    monkeypatch.setattr(indexer, "_update_links_for_changed", fail_links)
    with pytest.raises(RuntimeError, match="link insert failed"):
        await indexer.index_vault()

    assert fake.commits == 0
