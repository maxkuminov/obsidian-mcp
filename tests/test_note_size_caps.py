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


# ── move_note(rewrite_links=True): an over-cap rewrite is skipped ───────────


class _Row:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def _fake_session_returning(*result_rows):
    """A minimal `async_session` stand-in that replays canned `.all()` rows.

    `move_note_impl` issues, in order: the vault-index select, the backlink
    source select, then the two post-move UPDATEs.
    """
    calls = {"n": 0}

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
            i = calls["n"]
            calls["n"] += 1
            return Result(result_rows[i] if i < len(result_rows) else [])

        async def commit(self):
            return None

    return FakeSession


async def test_move_note_skips_over_cap_link_rewrite(offline, small_cap, monkeypatch):
    """An over-cap rewrite leaves that source byte-identical; the rest proceed."""
    from_rel = "old/target.md"
    to_rel = "new/deeper/a-much-longer-renamed-name.md"

    (offline / "old").mkdir()
    (offline / from_rel).write_text("moved note\n", encoding="utf-8")

    # Two backlink sources. `big.md` sits just under the cap, so expanding
    # `old/target` (10 chars) to the longer new path pushes it over; `small.md`
    # has room to spare.
    link = "See [[old/target]]\n"
    big = offline / "big.md"
    big.write_text("p" * (small_cap - len(link) - 3) + "\n" + link, encoding="utf-8")
    assert big.stat().st_size == small_cap - 2
    big_before = big.read_bytes()

    small = offline / "small.md"
    small.write_text(link, encoding="utf-8")

    monkeypatch.setattr(
        tools,
        "async_session",
        _fake_session_returning(
            [
                _Row(file_path=from_rel, id=1),
                _Row(file_path="big.md", id=2),
                _Row(file_path="small.md", id=3),
            ],
            [_Row(file_path="big.md"), _Row(file_path="small.md")],
        ),
    )

    seen: list[bytes | None] = []
    real_write = tools.write_file

    def spy(path, content, **kwargs):
        seen.append(kwargs.get("expected"))
        return real_write(path, content, **kwargs)

    monkeypatch.setattr(tools, "write_file", spy)

    result = await tools.move_note_impl(from_rel, to_rel, rewrite_links=True)

    # The move itself happened.
    assert "Moved" in result, result
    assert not (offline / from_rel).exists()
    assert (offline / to_rel).read_text(encoding="utf-8") == "moved note\n"

    # The over-cap source is untouched and named, with the limit, in the result.
    assert big.read_bytes() == big_before
    assert "big.md" in result
    assert str(small_cap) in result

    # The other source was rewritten, under the `expected=` conflict guard.
    assert "a-much-longer-renamed-name" in small.read_text(encoding="utf-8")
    assert seen == [link.encode("utf-8")]


# ── the size check must not displace the conflict check ─────────────────────


async def test_size_check_runs_before_the_conflict_detecting_write(
    offline, small_cap, monkeypatch
):
    """A concurrent write between read and write is still detected.

    The size check sits ahead of `write_file(..., expected=...)`; it must not
    have replaced it. (`tests/test_vault_mutation_safety.py` covers the
    conflict path in full; this pins the ordering.)
    """
    note = offline / "note.md"
    _write(note, "seed\n")
    seen = {}

    real_write = tools.write_file

    def spy(path, content, **kwargs):
        seen["expected"] = kwargs.get("expected")
        return real_write(path, content, **kwargs)

    monkeypatch.setattr(tools, "write_file", spy)
    result = await tools.edit_note_impl("note.md", "small enough")

    assert "Updated note" in result, result
    assert seen["expected"] == b"seed\n"
