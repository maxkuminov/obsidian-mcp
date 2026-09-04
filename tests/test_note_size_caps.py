"""Every note write tool bounds the *resulting* note at MAX_NOTE_BYTES.

`create_note` always did. `edit_note` (every mode) and `set_frontmatter` (both
directions) bounded only what they *read*, so a small edit could grow a note
without limit. That matters now that the MCP transport has a hard body ceiling:
the tool must be the thing that refuses a too-large write, with an actionable
message, so the transport only ever bounds unsupported shapes.

The matrix runs against a patched, small cap so the cases stay fast; one case
per tool exercises the real 10 MiB constant.
"""
import pytest

import src.mcp_server.tools as tools
from src.config import MAX_NOTE_BYTES
from src.mcp_server.auth import current_permission

SMALL_CAP = 4096


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    monkeypatch.setattr(tools.settings, "vault_path", str(tmp_path))

    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(tools, "_log_usage", noop)
    token = current_permission.set("readwrite")
    yield tmp_path
    current_permission.reset(token)


@pytest.fixture
def small_cap(monkeypatch):
    """Shrink the cap the tools enforce (and read against) for fast cases."""
    monkeypatch.setattr(tools, "MAX_NOTE_BYTES", SMALL_CAP)
    return SMALL_CAP


def _write(path, text):
    path.write_text(text, encoding="utf-8")
    stat = path.stat()
    return path.read_bytes(), stat.st_mtime_ns


# ── edit_note: every mutation branch ────────────────────────────────────────


OVER_CAP_EDITS = {
    "full_replace": ("seed\n", {"content": "x" * (SMALL_CAP + 1)}),
    "append": ("seed\n", {"content": "x" * SMALL_CAP, "append": True}),
    "find_replace": (
        "before HOLE after\n",
        {"content": "x" * SMALL_CAP, "find": "HOLE"},
    ),
    "replace_all": (
        "A A A\n",
        {"content": "x" * (SMALL_CAP // 2), "find": "A", "replace_all": True},
    ),
    "section": (
        "# Title\n\n## Tasks\n\nold body\n",
        {"content": "x" * SMALL_CAP, "section": "Tasks"},
    ),
}


@pytest.mark.parametrize("mode", sorted(OVER_CAP_EDITS))
async def test_edit_note_refuses_result_over_cap(offline, small_cap, mode):
    existing, kwargs = OVER_CAP_EDITS[mode]
    note = offline / "note.md"
    before, mtime = _write(note, existing)

    result = await tools.edit_note_impl("note.md", **kwargs)

    assert "Content too large" in result, result
    assert str(small_cap) in result
    assert note.read_bytes() == before
    assert note.stat().st_mtime_ns == mtime


@pytest.mark.parametrize("mode", sorted(OVER_CAP_EDITS))
async def test_edit_note_dry_run_refuses_result_over_cap(offline, small_cap, mode):
    existing, kwargs = OVER_CAP_EDITS[mode]
    note = offline / "note.md"
    before, mtime = _write(note, existing)

    result = await tools.edit_note_impl("note.md", dry_run=True, **kwargs)

    assert "Content too large" in result, result
    assert "---" not in result  # no diff header
    assert note.read_bytes() == before
    assert note.stat().st_mtime_ns == mtime


async def test_edit_note_result_exactly_at_cap_succeeds(offline, small_cap):
    note = offline / "note.md"
    _write(note, "seed\n")

    result = await tools.edit_note_impl("note.md", "x" * small_cap)

    assert "Updated note" in result, result
    assert note.stat().st_size == small_cap


async def test_edit_note_append_result_exactly_at_cap_succeeds(offline, small_cap):
    note = offline / "note.md"
    _write(note, "seed")  # 4 bytes; append adds "\n" + content

    result = await tools.edit_note_impl(
        "note.md", "x" * (small_cap - 5), append=True
    )

    assert "Updated note" in result, result
    assert note.stat().st_size == small_cap


async def test_edit_note_uses_the_real_10_mib_cap(offline):
    """The matrix patches the constant; this case proves the real one bites."""
    note = offline / "big.md"
    _write(note, "x" * MAX_NOTE_BYTES)

    result = await tools.edit_note_impl("big.md", "more", append=True)

    assert "Content too large" in result, result
    assert str(MAX_NOTE_BYTES) in result
    assert note.stat().st_size == MAX_NOTE_BYTES


# ── set_frontmatter: updates and remove-only ────────────────────────────────


async def test_set_frontmatter_refuses_result_over_cap(offline, small_cap):
    note = offline / "note.md"
    before, mtime = _write(note, "---\nkeep: yes\n---\n\nbody\n")

    result = await tools.set_frontmatter_impl(
        "note.md", updates={"big": "x" * small_cap}
    )

    assert "Content too large" in result, result
    assert str(small_cap) in result
    assert note.read_bytes() == before
    assert note.stat().st_mtime_ns == mtime


async def test_set_frontmatter_remove_only_at_cap_succeeds(offline, small_cap):
    """Removing a key can only shrink the note, so it stays allowed at the cap."""
    note = offline / "note.md"
    header = "---\ndrop: yes\nkeep: yes\n---\n\n"
    body = "y" * (small_cap - len(header))
    _write(note, header + body)
    assert note.stat().st_size == small_cap

    result = await tools.set_frontmatter_impl("note.md", remove=["drop"])

    assert "removed: drop" in result, result
    assert note.stat().st_size < small_cap


async def test_set_frontmatter_result_exactly_at_cap_succeeds(offline, small_cap):
    from src.services.vault import parse_frontmatter, serialize_frontmatter

    note = offline / "note.md"
    raw = "---\nkeep: yes\n---\n\n" + "y" * 100
    _write(note, raw)
    # Size the padding by asking the real serializer, so the case pins "exactly
    # at the cap" rather than an assumption about YAML formatting.
    fm, body = parse_frontmatter(raw)
    probe_len = 64
    probe = len(
        serialize_frontmatter({**fm, "pad": "z" * probe_len}, body).encode("utf-8")
    )
    result = await tools.set_frontmatter_impl(
        "note.md", updates={"pad": "z" * (probe_len + small_cap - probe)}
    )

    assert "set: pad" in result, result
    assert note.stat().st_size == small_cap


async def test_set_frontmatter_uses_the_real_10_mib_cap(offline):
    note = offline / "big.md"
    _write(note, "---\nkeep: yes\n---\n\n" + "y" * (MAX_NOTE_BYTES - 20))

    result = await tools.set_frontmatter_impl("big.md", updates={"pad": "z" * 4096})

    assert "Content too large" in result, result
    assert str(MAX_NOTE_BYTES) in result


# ── move_note(rewrite_links=True): an over-cap rewrite aborts the move ──────


class _Row:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _Journal:
    """Records what the fake session was asked to do, so a test can prove
    nothing mutated: `statements` holds every executed statement and `commits`
    counts the commits."""

    def __init__(self):
        self.statements = []
        self.commits = 0

    def mutating(self):
        """Executed statements that would change rows (the two post-move UPDATEs)."""
        from sqlalchemy.sql.expression import Delete, Insert, Update

        return [s for s in self.statements if isinstance(s, (Update, Insert, Delete))]


def _fake_session_returning(*result_rows):
    """A minimal `async_session` stand-in that replays canned `.all()` rows.

    `move_note_impl` issues, in order: the vault-index select, the backlink
    source select, then — only once it commits — the two post-move UPDATEs.
    Returns `(session_factory, journal)`.
    """
    calls = {"n": 0}
    journal = _Journal()

    class Result:
        def __init__(self, rows):
            self._rows = rows

        def all(self):
            return self._rows

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def execute(self, statement):
            journal.statements.append(statement)
            i = calls["n"]
            calls["n"] += 1
            return Result(result_rows[i] if i < len(result_rows) else [])

        async def commit(self):
            journal.commits += 1
            return None

    return FakeSession, journal


def _move_fixture(offline, small_cap, monkeypatch, *, over_cap: bool):
    """Set up a move with two backlink sources; `big.md` is over cap or not."""
    from_rel = "old/target.md"
    to_rel = "new/deeper/a-much-longer-renamed-name.md"

    (offline / "old").mkdir()
    (offline / from_rel).write_text("moved note\n", encoding="utf-8")

    # `big.md` sits just under the cap when `over_cap`, so expanding
    # `old/target` (10 chars) to the longer new path pushes it over. Otherwise
    # it has room to spare, like `small.md`.
    link = "See [[old/target]]\n"
    big = offline / "big.md"
    filler = (small_cap - len(link) - 3) if over_cap else 32
    big.write_text("p" * filler + "\n" + link, encoding="utf-8")
    if over_cap:
        assert big.stat().st_size == small_cap - 2

    small = offline / "small.md"
    small.write_text(link, encoding="utf-8")

    factory, journal = _fake_session_returning(
        [
            _Row(file_path=from_rel, id=1),
            _Row(file_path="big.md", id=2),
            _Row(file_path="small.md", id=3),
        ],
        [_Row(file_path="big.md"), _Row(file_path="small.md")],
    )
    monkeypatch.setattr(tools, "async_session", factory)

    seen: list[bytes | None] = []
    real_write = tools.write_file_at

    def spy(path, content, **kwargs):
        seen.append(kwargs.get("expected"))
        return real_write(path, content, **kwargs)

    monkeypatch.setattr(tools, "write_file_at", spy)
    return (
        from_rel,
        to_rel,
        big,
        small,
        big.read_bytes(),
        small.read_bytes(),
        seen,
        journal,
    )


async def test_move_note_aborts_when_a_link_rewrite_would_exceed_cap(
    offline, small_cap, monkeypatch
):
    """Over-cap source → the whole move is refused before anything mutates."""
    (
        from_rel,
        to_rel,
        big,
        small,
        big_before,
        small_before,
        seen,
        journal,
    ) = _move_fixture(offline, small_cap, monkeypatch, over_cap=True)

    result = await tools.move_note_impl(from_rel, to_rel, rewrite_links=True)

    # The error names the offending source and the limit, and does not claim
    # the move happened.
    assert "big.md" in result
    assert str(small_cap) in result
    assert "Moved" not in result

    # The filesystem is exactly as it was: the note never moved, and no source
    # — not even the under-cap one — was rewritten.
    assert (offline / from_rel).read_text(encoding="utf-8") == "moved note\n"
    assert not (offline / to_rel).exists()
    assert big.read_bytes() == big_before
    assert small.read_bytes() == small_before
    assert seen == []

    # And the DB saw only the two preflight SELECTs — no UPDATE, no commit.
    assert journal.mutating() == []
    assert journal.commits == 0


async def test_move_note_rewrites_unchanged_when_nothing_is_over_cap(
    offline, small_cap, monkeypatch
):
    """No over-cap source → move, DB update and rewrites behave as before."""
    (
        from_rel,
        to_rel,
        big,
        small,
        big_before,
        small_before,
        seen,
        journal,
    ) = _move_fixture(offline, small_cap, monkeypatch, over_cap=False)

    result = await tools.move_note_impl(from_rel, to_rel, rewrite_links=True)

    assert "Moved" in result, result
    assert "rewrote 2 link(s) across 2 note(s)" in result
    assert "warning" not in result
    assert not (offline / from_rel).exists()
    assert (offline / to_rel).read_text(encoding="utf-8") == "moved note\n"

    # Both sources were rewritten...
    assert "a-much-longer-renamed-name" in big.read_text(encoding="utf-8")
    assert "a-much-longer-renamed-name" in small.read_text(encoding="utf-8")
    # ...each under the `expected=` conflict guard, carrying the pre-move bytes.
    assert seen == [big_before, small_before]

    # The move's DB updates still ran and committed.
    assert len(journal.mutating()) == 2
    assert journal.commits == 1


# ── move_note: the preflight is also bounded in aggregate ───────────────────


async def test_move_note_aborts_when_the_preflight_would_hold_too_much(
    offline, small_cap, monkeypatch
):
    """Many small sources can blow the memory budget the per-note cap misses.

    The preflight retains every original *and* its rewrite before mutating, so
    the bound that matters is the sum, not the largest source. Three sources,
    each comfortably under `MAX_NOTE_BYTES`, exceed a patched aggregate bound.
    """
    monkeypatch.setattr(tools, "MAX_MOVE_REWRITE_BYTES", 900)

    from_rel = "old/target.md"
    to_rel = "new/deeper/a-much-longer-renamed-name.md"
    (offline / "old").mkdir()
    (offline / from_rel).write_text("moved note\n", encoding="utf-8")

    # ~201 bytes each: two fit inside the 900-byte budget (originals +
    # rewrites), the third cannot.
    link = "See [[old/target]]\n"
    names = ["a.md", "b.md", "c.md"]
    sources = []
    for name in names:
        note = offline / name
        note.write_text("p" * 181 + "\n" + link, encoding="utf-8")
        assert note.stat().st_size < small_cap
        sources.append((note, note.read_bytes()))

    factory, journal = _fake_session_returning(
        [_Row(file_path=from_rel, id=1)]
        + [_Row(file_path=n, id=i) for i, n in enumerate(names, start=2)],
        [_Row(file_path=n) for n in names],
    )
    monkeypatch.setattr(tools, "async_session", factory)

    seen: list[bytes | None] = []
    real_write = tools.write_file_at

    def spy(path, content, **kwargs):
        seen.append(kwargs.get("expected"))
        return real_write(path, content, **kwargs)

    monkeypatch.setattr(tools, "write_file_at", spy)

    result = await tools.move_note_impl(from_rel, to_rel, rewrite_links=True)

    # The error names how many notes were involved and the limit it hit.
    assert "3 notes" in result, result
    assert "900" in result, result
    assert "Moved" not in result

    # Nothing moved, nothing rewritten, nothing written at all.
    assert (offline / from_rel).read_text(encoding="utf-8") == "moved note\n"
    assert not (offline / to_rel).exists()
    for note, before in sources:
        assert note.read_bytes() == before
    assert seen == []

    # And the DB saw only the two preflight SELECTs — no UPDATE, no commit.
    assert journal.mutating() == []
    assert journal.commits == 0


async def test_move_note_within_the_aggregate_bound_still_rewrites(
    offline, small_cap, monkeypatch
):
    """The same three sources under a bound that fits: the move goes through."""
    monkeypatch.setattr(tools, "MAX_MOVE_REWRITE_BYTES", 10_000)

    from_rel = "old/target.md"
    to_rel = "new/deeper/a-much-longer-renamed-name.md"
    (offline / "old").mkdir()
    (offline / from_rel).write_text("moved note\n", encoding="utf-8")

    link = "See [[old/target]]\n"
    names = ["a.md", "b.md", "c.md"]
    for name in names:
        (offline / name).write_text("p" * 181 + "\n" + link, encoding="utf-8")

    factory, journal = _fake_session_returning(
        [_Row(file_path=from_rel, id=1)]
        + [_Row(file_path=n, id=i) for i, n in enumerate(names, start=2)],
        [_Row(file_path=n) for n in names],
    )
    monkeypatch.setattr(tools, "async_session", factory)

    result = await tools.move_note_impl(from_rel, to_rel, rewrite_links=True)

    assert "rewrote 3 link(s) across 3 note(s)" in result, result
    for name in names:
        assert "a-much-longer-renamed-name" in (offline / name).read_text(
            encoding="utf-8"
        )
    assert journal.commits == 1


# ── move_note: one source's rewrites are bounded too ────────────────────────


async def test_move_note_aborts_when_one_source_holds_too_many_rewrites(
    offline, monkeypatch
):
    """`MAX_LINKS_PER_NOTE` applies to the write side, not only extraction.

    The aggregate byte bound above is about the *number of sources*; this is
    about one source. Nothing capped how many links a single note could have
    rewritten, so a note of `[[Old]] ` at `MAX_NOTE_BYTES` planned ~1.7
    million of them — and the indexer would then persist only the first
    10,000, leaving the graph asserting a link set the bytes contradict.

    Refused, not truncated, and refused *before* phase 2, exactly as the
    `MAX_NOTE_BYTES` and `MAX_MOVE_REWRITE_BYTES` preflights are: rewriting
    the first N and stopping leaves the rest of the note pointing at the path
    the move just vacated and reports the move as a success.
    """
    monkeypatch.setattr(tools, "MAX_LINKS_PER_NOTE", 3)

    from_rel = "old/target.md"
    to_rel = "new/renamed.md"
    (offline / "old").mkdir()
    (offline / from_rel).write_text("moved note\n", encoding="utf-8")

    hub = offline / "hub.md"
    hub.write_text("[[old/target]] " * 4, encoding="utf-8")
    before = hub.read_bytes()

    factory, journal = _fake_session_returning(
        [_Row(file_path=from_rel, id=1), _Row(file_path="hub.md", id=2)],
        [_Row(file_path="hub.md")],
    )
    monkeypatch.setattr(tools, "async_session", factory)

    seen: list[bytes | None] = []
    real_write = tools.write_file_at

    def spy(path, content, **kwargs):
        seen.append(kwargs.get("expected"))
        return real_write(path, content, **kwargs)

    monkeypatch.setattr(tools, "write_file_at", spy)

    result = await tools.move_note_impl(from_rel, to_rel, rewrite_links=True)

    # Names the source and the cap, so the agent knows which note to split.
    assert "Move aborted" in result, result
    assert "hub.md" in result, result
    assert "MAX_LINKS_PER_NOTE=3" in result, result
    assert "4 links" in result, result
    assert "Moved" not in result

    # Nothing moved, nothing rewritten, nothing written at all.
    assert (offline / from_rel).read_text(encoding="utf-8") == "moved note\n"
    assert not (offline / to_rel).exists()
    assert hub.read_bytes() == before
    assert seen == []

    # And the DB saw only the two preflight SELECTs — no UPDATE, no commit.
    assert journal.mutating() == []
    assert journal.commits == 0


async def test_move_note_at_the_rewrite_cap_still_goes_through(offline, monkeypatch):
    """The refusal is "more than the cap", not "at" it."""
    monkeypatch.setattr(tools, "MAX_LINKS_PER_NOTE", 4)

    from_rel = "old/target.md"
    to_rel = "new/renamed.md"
    (offline / "old").mkdir()
    (offline / from_rel).write_text("moved note\n", encoding="utf-8")
    hub = offline / "hub.md"
    hub.write_text("[[old/target]] " * 4, encoding="utf-8")

    factory, journal = _fake_session_returning(
        [_Row(file_path=from_rel, id=1), _Row(file_path="hub.md", id=2)],
        [_Row(file_path="hub.md")],
    )
    monkeypatch.setattr(tools, "async_session", factory)

    result = await tools.move_note_impl(from_rel, to_rel, rewrite_links=True)

    assert "rewrote 4 link(s)" in result, result
    assert hub.read_text(encoding="utf-8") == "[[new/renamed]] " * 4
    assert journal.commits == 1


# ── the size check must not displace the conflict check ─────────────────────


async def test_size_check_runs_before_the_conflict_detecting_write(
    offline, small_cap, monkeypatch
):
    """A concurrent write between read and write is still detected.

    The size check sits ahead of `write_file_at(..., expected=...)`; it must not
    have replaced it. (`tests/test_vault_mutation_safety.py` covers the
    conflict path in full; this pins the ordering.)
    """
    note = offline / "note.md"
    _write(note, "seed\n")
    seen = {}

    real_write = tools.write_file_at

    def spy(path, content, **kwargs):
        seen["expected"] = kwargs.get("expected")
        return real_write(path, content, **kwargs)

    monkeypatch.setattr(tools, "write_file_at", spy)
    result = await tools.edit_note_impl("note.md", "small enough")

    assert "Updated note" in result, result
    assert seen["expected"] == b"seed\n"
