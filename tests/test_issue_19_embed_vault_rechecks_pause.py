"""Regression test for GitHub issue #19.

`embed_vault` selects every note whose embedding is missing or stale, then
loops over the result embedding one note per iteration. Originally that loop
never rechecked the panel-driven pause flag (`_is_paused()`), so once an embed
pass was underway a panel action (e.g. **Settings -> Reset embeddings**, which
sets `indexer_paused = True`) could not actually stop it: the loop ground
through the entire backlog before the next periodic tick noticed the pause.

The fix rechecks `_is_paused()` at the top of every iteration and breaks out of
the loop when it flips true, so an in-flight embed pass stops promptly.

This test drives the real `embed_vault` against a temp vault on disk with a
fake `async_session`. It stubs out the actual embedding work (`embed_note`) and
toggles the pause flag, then asserts the loop stops early instead of embedding
every note.

Fully offline: no DB, no network, no embedding provider. The session is faked
and `embed_note` is monkeypatched, so neither pgvector nor Ollama/OpenAI are
touched.
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

from sqlalchemy.sql.elements import TextClause  # noqa: E402

import src.services.indexer as indexer  # noqa: E402


class _Row:
    """Mimics one row from the unembedded SELECT."""

    def __init__(self, id, file_path, content_hash):
        self.id = id
        self.file_path = file_path
        self.content_hash = content_hash


class _FakeResult:
    def __init__(self, rows=None, scalar_one=None):
        self._rows = rows or []
        self._scalar_one = scalar_one

    def fetchall(self):
        return self._rows

    def all(self):
        return self._rows

    def scalar(self):
        return 0

    def scalar_one(self):
        # Returned for the per-note `select(NoteMetadata)` lookup. Any object
        # works here because `embed_note` is monkeypatched in the test.
        return self._scalar_one


class _FakeSession:
    """Returns the unembedded rows for the initial SELECT, inert otherwise."""

    def __init__(self, rows):
        self._rows = rows
        self._returned_unembedded = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, stmt, params=None):
        # The first TextClause SELECT is the "find unembedded notes" query.
        if isinstance(stmt, TextClause) and "embedded_content_hash" in stmt.text \
                and "SELECT" in stmt.text.upper():
            if not self._returned_unembedded:
                self._returned_unembedded = True
                return _FakeResult(rows=list(self._rows))
            return _FakeResult()
        # The per-note `select(NoteMetadata).where(...)` lookup.
        return _FakeResult(scalar_one=object())

    async def commit(self):
        return None

    async def rollback(self):
        return None


def _make_rows(vault, n):
    rows = []
    for i in range(n):
        rel = f"note{i}.md"
        (vault / rel).write_text(f"# Note {i}\n\nbody {i}\n", encoding="utf-8")
        rows.append(_Row(id=i + 1, file_path=rel, content_hash=f"hash{i}"))
    return rows


@pytest.mark.asyncio
async def test_embed_vault_breaks_when_paused_before_loop(monkeypatch, tmp_path):
    """If paused before the loop runs, no notes are embedded."""
    vault = tmp_path / "vault"
    vault.mkdir()
    rows = _make_rows(vault, 5)

    monkeypatch.setattr(indexer.settings, "vault_path", str(vault), raising=False)
    monkeypatch.setattr(indexer, "async_session", lambda: _FakeSession(rows))
    monkeypatch.setattr(indexer.settings, "embedding_exclude_patterns", [], raising=False)

    embedded = []

    async def _fake_embed_note(session, note, content):
        embedded.append(note)
        return 1

    monkeypatch.setattr(indexer, "embed_note", _fake_embed_note)
    # Paused for the entire pass.
    monkeypatch.setattr(indexer, "_is_paused", lambda: True)

    await indexer.embed_vault()

    assert embedded == [], (
        "embed_vault embedded notes despite the indexer being paused — the "
        "loop never rechecked the pause flag (issue #19 regression)"
    )


@pytest.mark.asyncio
async def test_embed_vault_stops_early_when_pause_flips_mid_pass(monkeypatch, tmp_path):
    """Pausing mid-pass stops further embedding promptly."""
    vault = tmp_path / "vault"
    vault.mkdir()
    rows = _make_rows(vault, 5)

    monkeypatch.setattr(indexer.settings, "vault_path", str(vault), raising=False)
    monkeypatch.setattr(indexer, "async_session", lambda: _FakeSession(rows))
    monkeypatch.setattr(indexer.settings, "embedding_exclude_patterns", [], raising=False)

    embedded = []
    paused = {"value": False}

    async def _fake_embed_note(session, note, content):
        embedded.append(note)
        # After the first note is embedded, simulate a panel-driven pause.
        paused["value"] = True
        return 1

    monkeypatch.setattr(indexer, "embed_note", _fake_embed_note)
    monkeypatch.setattr(indexer, "_is_paused", lambda: paused["value"])

    await indexer.embed_vault()

    # Without the fix the loop runs to completion (all 5 notes). With the fix it
    # embeds the first note, the pause flips, and the next iteration breaks.
    assert len(embedded) == 1, (
        f"expected the embed loop to stop after the pause flipped, but it "
        f"embedded {len(embedded)} notes (issue #19 regression)"
    )
