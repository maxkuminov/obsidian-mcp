"""`expected_hash` on the five note write tools (#205, slice B).

Slice A defined the digest, the six refusal codes and the precedence ladder and
guarded nothing. This is where the ladder reaches the destructive path: every
tool that can overwrite or destroy a note now accepts the caller's hash for the
bytes it read, and refuses — writing **nothing** — when the file has moved on.

What is pinned here, all of it offline (a `tmp_path` vault, `_log_usage`
stubbed or recorded, a fake session where `move_note` needs one):

  * **the refusal writes nothing.** Every stale/malformed/unavailable/required
    case asserts the file is byte-identical afterwards, and the delete cases
    additionally assert that `.trash` was never created.
  * **ordering is observable, so it is asserted.** A stale hash beats the
    `dry_run` diff, the `set_frontmatter` no-op, the frontmatter-defect report
    and the size cap; a malformed hash beats not-found, a symlinked leaf, the
    over-cap refusal and `create_note`'s own `no_incumbent`.
  * **the two windows are distinguishable by code.** A matching precondition
    does not disable the in-call `expected=` compare, and when that one fires
    its prose is byte-unchanged with a `concurrent_write` sentinel appended.
  * **`move_note`'s reported hash is what is on disk**, across the three
    post-rename outcomes the design separates: the moved note's own rewrite
    published, the same rewrite failing without observing a change, and that
    rewrite losing the in-call conflict (no hash at all, and never
    `nothing_written`).
  * **an unguarded call is exactly today's call**, including the silent
    overwrite this capability exists to make avoidable and including an
    over-cap file, which must not start failing merely because it is too large
    to hash.
"""

import hashlib
import json
import os
import tempfile

os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")
os.chdir(tempfile.gettempdir())

import pytest  # noqa: E402

import src.mcp_server.tools as tools  # noqa: E402
from src.auth.session import current_user_id  # noqa: E402
from src.mcp_server.auth import current_permission  # noqa: E402
from src.services import refusals  # noqa: E402
from src.services import vault as vault_service  # noqa: E402

pytestmark = pytest.mark.asyncio


# ── fixtures and helpers ────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _no_usage_log(monkeypatch):
    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(tools, "_log_usage", _noop)


@pytest.fixture(autouse=True)
def _writable():
    perm = current_permission.set("readwrite")
    uid = current_user_id.set(None)
    yield
    current_user_id.reset(uid)
    current_permission.reset(perm)


@pytest.fixture
def vault(monkeypatch, tmp_path):
    monkeypatch.setattr(tools.settings, "vault_path", str(tmp_path))
    monkeypatch.setattr(vault_service.settings, "vault_path", str(tmp_path))
    monkeypatch.setattr(tools.settings, "write_precondition_required", False)
    vault_service.clear_user_vault_cache()
    return tmp_path


@pytest.fixture
def required_mode(monkeypatch):
    """`WRITE_PRECONDITION_REQUIRED` on — a deployment lever, never per call."""
    monkeypatch.setattr(tools.settings, "write_precondition_required", True)


def seed(vault, name: str, data: bytes = b"# A\nbody\n") -> str:
    (vault / name).write_bytes(data)
    return name


def digest(vault, name: str) -> str:
    """The digest a caller computes from the file itself."""
    return "sha256:" + hashlib.sha256((vault / name).read_bytes()).hexdigest()


@pytest.mark.parametrize("operation", ["move", "soft_delete", "permanent_delete"])
@pytest.mark.parametrize("read_error", [PermissionError(13, "Permission denied"), OSError(40, "Too many symbolic links")])
async def test_guarded_move_and_delete_return_read_errors_without_mutation(
    vault, monkeypatch, operation, read_error
):
    name = seed(vault, "source.md")
    expected = digest(vault, name)
    original = (vault / name).read_bytes()

    def fail_read(*args, **kwargs):
        raise read_error

    monkeypatch.setattr(tools, "_read_incumbent", fail_read)
    if operation == "move":
        result = await tools.move_note_impl(name, "destination.md", expected_hash=expected)
    else:
        result = await tools.delete_note_impl(
            name, permanent=operation == "permanent_delete", expected_hash=expected
        )
    assert result.startswith("Failed to read source.md:")
    assert (vault / name).read_bytes() == original
    assert not (vault / "destination.md").exists()
    assert not (vault / ".trash").exists()


def sentinel(text: str) -> dict:
    """The refusal's machine-readable final line, parsed."""
    last = text.rsplit("\n", 1)[-1]
    assert last.startswith(f"{refusals.SENTINEL} "), text
    return json.loads(last[len(refusals.SENTINEL) + 1 :])


def reported_hash(result: str) -> str:
    """The `content_hash` a successful write ends its result with."""
    marker = "content_hash: "
    assert marker in result, result
    return result.rsplit(marker, 1)[1].splitlines()[0].strip()


STALE = "sha256:" + "0" * 64
MALFORMED = [
    ("bare hex", "0" * 64),
    ("uppercase", "sha256:" + "A" * 64),
    ("wrong length", "sha256:" + "0" * 63),
    ("unknown algorithm", "sha1:" + "0" * 40),
    ("surrounding whitespace", " sha256:" + "0" * 64 + " "),
]


# ══════════════════════════════════════════════════════════════════════════
# 1. edit_note — every mode, all three answers
# ══════════════════════════════════════════════════════════════════════════


EDIT_MODES = [
    ("full replace", {}),
    ("append", {"append": True}),
    ("find", {"find": "body"}),
    ("section", {"section": "A"}),
    ("replace_frontmatter", {"replace_frontmatter": True}),
    ("dry_run", {"dry_run": True}),
]


@pytest.mark.parametrize("label, kwargs", EDIT_MODES)
async def test_edit_note_stale_hash_refuses_in_every_mode(vault, label, kwargs):
    """The mode does not matter: the comparison runs before mode dispatch.

    `dry_run` is in the list on purpose — a unified diff computed against a
    base the caller does not hold is a wrong answer, not a cheap one.
    """
    name = seed(vault, "n.md", b"# A\nbody\n")
    before = (vault / name).read_bytes()

    out = await tools.edit_note_impl(
        name, "replacement", expected_hash=STALE, **kwargs
    )

    payload = sentinel(out)
    assert payload["code"] == refusals.STALE_PRECONDITION
    assert payload["path"] == name
    assert payload["current_hash"] == digest(vault, name)
    assert payload["nothing_written"] is True
    assert (vault / name).read_bytes() == before
    # No note content in the refusal — not an excerpt, not a diff, not a length.
    assert "body" not in out
    assert "@@" not in out


@pytest.mark.parametrize("label, kwargs", EDIT_MODES)
async def test_edit_note_matching_hash_proceeds_in_every_mode(vault, label, kwargs):
    name = seed(vault, "n.md", b"# A\nbody\n")

    out = await tools.edit_note_impl(
        name, "replacement", expected_hash=digest(vault, name), **kwargs
    )

    assert not refusals.has_sentinel(out), out
    if kwargs.get("dry_run"):
        # Published nothing, so it reports no hash and the file is untouched.
        assert "content_hash" not in out
        assert (vault / name).read_bytes() == b"# A\nbody\n"
    else:
        assert reported_hash(out) == digest(vault, name)


async def test_an_omitted_hash_keeps_todays_silent_overwrite(vault):
    """The compatibility claim, asserted as behaviour rather than promised."""
    name = seed(vault, "n.md", b"# A\nold\n")
    (vault / name).write_bytes(b"# A\nsomebody elses edit\n")

    out = await tools.edit_note_impl(name, "mine", replace_frontmatter=True)

    assert not refusals.has_sentinel(out), out
    assert (vault / name).read_bytes() == b"mine"


@pytest.mark.parametrize("label, value", MALFORMED)
async def test_edit_note_malformed_hash_is_its_own_code(vault, label, value):
    name = seed(vault, "n.md")
    before = (vault / name).read_bytes()

    out = await tools.edit_note_impl(name, "x", expected_hash=value)

    payload = sentinel(out)
    assert payload["code"] == refusals.MALFORMED_PRECONDITION
    assert payload["nothing_written"] is True
    assert "current_hash" not in payload
    assert "sha256:<64 lowercase hex>" in out
    assert (vault / name).read_bytes() == before


async def test_a_stale_hash_beats_the_dry_run_diff(vault):
    name = seed(vault, "n.md", b"# A\nbody\n")

    out = await tools.edit_note_impl(
        name, "different", dry_run=True, expected_hash=STALE
    )

    assert sentinel(out)["code"] == refusals.STALE_PRECONDITION
    assert "---" not in out.split("\n")[0]
    assert "+++" not in out


async def test_a_stale_hash_beats_the_no_changes_answer(vault):
    """Identical content is still refused: the base is what is being asserted."""
    name = seed(vault, "n.md", b"same\n")

    out = await tools.edit_note_impl(
        name, "same\n", replace_frontmatter=True, dry_run=True, expected_hash=STALE
    )

    assert sentinel(out)["code"] == refusals.STALE_PRECONDITION
    assert "No changes" not in out


async def test_a_stale_hash_beats_the_frontmatter_defect_report(vault):
    """A defect report on bytes the caller has not seen sends it to repair the
    wrong thing, so the precondition is answered first."""
    name = seed(vault, "n.md", b"---\nbroken: [\n---\n# A\nbody\n")
    before = (vault / name).read_bytes()

    out = await tools.edit_note_impl(name, "x", section="A", expected_hash=STALE)

    assert sentinel(out)["code"] == refusals.STALE_PRECONDITION
    assert "malformed frontmatter" not in out
    assert (vault / name).read_bytes() == before


async def test_a_stale_hash_beats_the_result_size_cap(vault, monkeypatch):
    """Ordering: the comparison runs before the size cap, not after it."""
    name = seed(vault, "n.md", b"tiny\n")
    monkeypatch.setattr(tools, "MAX_NOTE_BYTES", 8)

    out = await tools.edit_note_impl(
        name, "x" * 100, replace_frontmatter=True, expected_hash=STALE
    )

    assert sentinel(out)["code"] == refusals.STALE_PRECONDITION
    assert "Content too large" not in out


async def test_a_section_write_is_bound_to_the_whole_file(vault):
    """D5's declared trade: the narrowest mode is the most conflict-prone.

    The caller read `## Tasks`, someone else edited `## Notes`, and the section
    write is refused — because a body-only digest could not have told the
    caller that `#N` still names the same section.
    """
    name = seed(vault, "n.md", b"## Tasks\nt1\n\n## Notes\nn1\n")
    read_hash = digest(vault, name)
    (vault / name).write_bytes(b"## Tasks\nt1\n\n## Notes\nn1 plus more\n")

    out = await tools.edit_note_impl(
        name, "t2\n", section="Tasks", expected_hash=read_hash
    )

    assert sentinel(out)["code"] == refusals.STALE_PRECONDITION
    assert b"t1" in (vault / name).read_bytes()


async def test_a_section_write_with_the_current_whole_file_hash_proceeds(vault):
    name = seed(vault, "n.md", b"## Tasks\nt1\n\n## Notes\nn1\n")

    out = await tools.edit_note_impl(
        name, "t2\n", section="Tasks", expected_hash=digest(vault, name)
    )

    assert reported_hash(out) == digest(vault, name)
    # A section body runs to the next heading, blank line included, and is
    # replaced whole — so the blank line is gone, which is the documented
    # behaviour and not the precondition's doing.
    assert (vault / name).read_bytes() == b"## Tasks\nt2\n## Notes\nn1\n"


async def test_a_reported_hash_binds_the_next_write(vault):
    """The write→write chain the reported hash exists to make guardable."""
    name = seed(vault, "n.md", b"one\n")

    first = await tools.edit_note_impl(
        name, "two\n", replace_frontmatter=True, expected_hash=digest(vault, name)
    )
    second = await tools.edit_note_impl(
        name, "three\n", replace_frontmatter=True, expected_hash=reported_hash(first)
    )

    assert not refusals.has_sentinel(second), second
    assert (vault / name).read_bytes() == b"three\n"


# ══════════════════════════════════════════════════════════════════════════
# 2. the two windows, told apart by code
# ══════════════════════════════════════════════════════════════════════════


def _clobber_before_publish(monkeypatch, path, third_party: bytes):
    """Make a third writer land between this call's read and its publication."""
    real = tools.write_file_at

    def wrapper(target, content, **kwargs):
        if kwargs.get("expected") is not None:
            path.write_bytes(third_party)
        return real(target, content, **kwargs)

    monkeypatch.setattr(tools, "write_file_at", wrapper)


async def test_a_matching_precondition_does_not_disable_the_in_call_compare(
    vault, monkeypatch
):
    name = seed(vault, "n.md", b"base\n")
    good = digest(vault, name)
    _clobber_before_publish(monkeypatch, vault / name, b"theirs\n")

    out = await tools.edit_note_impl(
        name, "mine\n", replace_frontmatter=True, expected_hash=good
    )

    payload = sentinel(out)
    assert payload["code"] == refusals.CONCURRENT_WRITE
    assert payload["nothing_written"] is True
    # The prose half is byte-unchanged, so every existing assertion holds.
    assert out.split("\n")[0] == "File changed while editing: n.md"
    assert (vault / name).read_bytes() == b"theirs\n"


async def test_the_two_conflict_windows_carry_different_codes(vault, monkeypatch):
    """An agent must be able to tell "you were stale on arrival" from "the file
    moved while I was working", because the remedies differ."""
    name = seed(vault, "n.md", b"base\n")
    good = digest(vault, name)

    stale_out = await tools.edit_note_impl(
        name, "x", replace_frontmatter=True, expected_hash=STALE
    )
    _clobber_before_publish(monkeypatch, vault / name, b"theirs\n")
    in_call_out = await tools.edit_note_impl(
        name, "x", replace_frontmatter=True, expected_hash=good
    )

    assert sentinel(stale_out)["code"] == refusals.STALE_PRECONDITION
    assert "current_hash" in sentinel(stale_out)
    assert sentinel(in_call_out)["code"] == refusals.CONCURRENT_WRITE
    assert "current_hash" not in sentinel(in_call_out)


async def test_set_frontmatter_renders_the_in_call_conflict_too(vault, monkeypatch):
    name = seed(vault, "n.md", b"---\na: 1\n---\nbody\n")
    _clobber_before_publish(monkeypatch, vault / name, b"theirs\n")

    out = await tools.set_frontmatter_impl(name, updates={"a": 2})

    assert sentinel(out)["code"] == refusals.CONCURRENT_WRITE
    assert out.split("\n")[0].startswith("File changed while editing:")


# ══════════════════════════════════════════════════════════════════════════
# 3. set_frontmatter
# ══════════════════════════════════════════════════════════════════════════


async def test_set_frontmatter_stale_hash_refuses(vault):
    name = seed(vault, "n.md", b"---\na: 1\n---\nbody\n")
    before = (vault / name).read_bytes()

    out = await tools.set_frontmatter_impl(name, updates={"a": 2}, expected_hash=STALE)

    payload = sentinel(out)
    assert payload["code"] == refusals.STALE_PRECONDITION
    assert payload["current_hash"] == digest(vault, name)
    assert (vault / name).read_bytes() == before


async def test_set_frontmatter_matching_hash_reports_the_published_hash(vault):
    name = seed(vault, "n.md", b"---\na: 1\n---\nbody\n")

    out = await tools.set_frontmatter_impl(
        name, updates={"a": 2}, expected_hash=digest(vault, name)
    )

    assert reported_hash(out) == digest(vault, name)
    assert b"a: 2" in (vault / name).read_bytes()


async def test_a_stale_hash_beats_the_set_frontmatter_no_op(vault):
    """`updates` that change nothing would report "no changes" — against a base
    the caller does not hold, which is the wrong answer."""
    name = seed(vault, "n.md", b"---\na: 1\n---\nbody\n")

    out = await tools.set_frontmatter_impl(name, updates={"a": 1}, expected_hash=STALE)

    assert sentinel(out)["code"] == refusals.STALE_PRECONDITION
    assert "No changes" not in out


async def test_a_stale_hash_beats_the_empty_updates_no_op(vault):
    name = seed(vault, "n.md", b"---\na: 1\n---\nbody\n")

    out = await tools.set_frontmatter_impl(name, expected_hash=STALE)

    assert sentinel(out)["code"] == refusals.STALE_PRECONDITION


async def test_a_stale_hash_beats_the_set_frontmatter_defect_report(vault):
    name = seed(vault, "n.md", b"---\nbroken: [\n---\nbody\n")

    out = await tools.set_frontmatter_impl(name, updates={"a": 1}, expected_hash=STALE)

    assert sentinel(out)["code"] == refusals.STALE_PRECONDITION
    assert "malformed frontmatter" not in out


async def test_a_set_frontmatter_no_op_reports_no_hash(vault):
    """Nothing was published, so there is nothing to name."""
    name = seed(vault, "n.md", b"---\na: 1\n---\nbody\n")

    out = await tools.set_frontmatter_impl(
        name, updates={"a": 1}, expected_hash=digest(vault, name)
    )

    assert out.startswith("No changes for")
    assert "content_hash" not in out


@pytest.mark.parametrize("label, value", MALFORMED)
async def test_set_frontmatter_malformed_hash(vault, label, value):
    name = seed(vault, "n.md", b"---\na: 1\n---\nbody\n")
    before = (vault / name).read_bytes()

    out = await tools.set_frontmatter_impl(name, updates={"a": 2}, expected_hash=value)

    assert sentinel(out)["code"] == refusals.MALFORMED_PRECONDITION
    assert (vault / name).read_bytes() == before


# ══════════════════════════════════════════════════════════════════════════
# 4. create_note — the argument it can never honour
# ══════════════════════════════════════════════════════════════════════════


async def test_create_note_answers_a_hash_rather_than_rejecting_it(vault):
    """A signature that refused the argument would answer with a protocol
    error, which is the opposite of the contract this change promises."""
    out = await tools.create_note_impl("fresh.md", "body\n", expected_hash=STALE)

    payload = sentinel(out)
    assert payload["code"] == refusals.NO_INCUMBENT
    assert payload["path"] == "fresh.md"
    assert payload["nothing_written"] is True
    assert "without expected_hash" in out
    assert not (vault / "fresh.md").exists()


@pytest.mark.parametrize("label, value", MALFORMED)
async def test_malformed_beats_no_incumbent_on_create_note(vault, label, value):
    out = await tools.create_note_impl("fresh.md", "body\n", expected_hash=value)

    assert sentinel(out)["code"] == refusals.MALFORMED_PRECONDITION
    assert not (vault / "fresh.md").exists()


async def test_create_note_without_a_hash_reports_the_published_hash(vault):
    out = await tools.create_note_impl("fresh.md", "body\n")

    assert out.startswith("Created note: fresh.md")
    assert reported_hash(out) == digest(vault, "fresh.md")


async def test_create_note_is_exempt_from_required_mode(vault, required_mode):
    out = await tools.create_note_impl("fresh.md", "body\n")

    assert not refusals.has_sentinel(out), out
    assert (vault / "fresh.md").read_bytes() == b"body\n"


# ══════════════════════════════════════════════════════════════════════════
# 5. delete_note — both modes
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("permanent", [False, True])
async def test_delete_note_stale_hash_refuses_in_both_modes(vault, permanent):
    name = seed(vault, "n.md", b"keep me\n")

    out = await tools.delete_note_impl(name, permanent=permanent, expected_hash=STALE)

    payload = sentinel(out)
    assert payload["code"] == refusals.STALE_PRECONDITION
    assert payload["current_hash"] == digest(vault, name)
    assert (vault / name).read_bytes() == b"keep me\n"
    # Nothing was moved out of the way either.
    assert not (vault / ".trash").exists()


@pytest.mark.parametrize("permanent", [False, True])
async def test_delete_note_matching_hash_proceeds_and_reports_no_hash(
    vault, permanent
):
    name = seed(vault, "n.md", b"go\n")

    out = await tools.delete_note_impl(
        name, permanent=permanent, expected_hash=digest(vault, name)
    )

    assert not refusals.has_sentinel(out), out
    assert "content_hash" not in out
    assert not (vault / name).exists()


@pytest.mark.parametrize("label, value", MALFORMED)
async def test_delete_note_malformed_hash(vault, label, value):
    name = seed(vault, "n.md", b"keep me\n")

    out = await tools.delete_note_impl(name, expected_hash=value)

    assert sentinel(out)["code"] == refusals.MALFORMED_PRECONDITION
    assert (vault / name).exists()
    assert not (vault / ".trash").exists()


async def test_an_unguarded_delete_reads_nothing(vault, monkeypatch):
    """A tool that reads nothing today must not start reading when unguarded."""
    name = seed(vault, "n.md", b"go\n")
    calls = {"n": 0}
    real = tools._read_incumbent

    def counting(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(tools, "_read_incumbent", counting)

    await tools.delete_note_impl(name, permanent=True)

    assert calls["n"] == 0


# ══════════════════════════════════════════════════════════════════════════
# 6. move_note
# ══════════════════════════════════════════════════════════════════════════


class _Row:
    def __init__(self, **fields):
        self.__dict__.update(fields)


def _fake_session(*result_rows):
    """Successive `execute` results; anything past the list is empty."""
    calls = {"n": 0}

    class Result:
        def __init__(self, rows):
            self._rows = rows

        def all(self):
            return self._rows

        def scalar_one_or_none(self):
            return self._rows[0] if self._rows else None

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

    FakeSession.calls = calls
    return FakeSession


def _no_session():
    """A session factory that fails the test if a refused move ever opens one."""

    class Forbidden:
        async def __aenter__(self):  # pragma: no cover - must not run
            raise AssertionError("a refused move must do no database work")

        async def __aexit__(self, *args):  # pragma: no cover
            return None

    return Forbidden


async def test_move_note_stale_hash_refuses_before_the_rename(vault, monkeypatch):
    seed(vault, "Old.md", b"moved\n")
    monkeypatch.setattr(tools, "async_session", _no_session())

    out = await tools.move_note_impl("Old.md", "New.md", expected_hash=STALE)

    payload = sentinel(out)
    assert payload["code"] == refusals.STALE_PRECONDITION
    assert payload["path"] == "Old.md"
    assert payload["current_hash"] == digest(vault, "Old.md")
    assert payload["nothing_written"] is True
    assert (vault / "Old.md").read_bytes() == b"moved\n"
    assert not (vault / "New.md").exists()


async def test_a_move_refusal_says_backlink_sources_are_not_bound(vault, monkeypatch):
    """A precondition covering one of N files while implying all of them is
    worse than none, so the scope is stated rather than left to be inferred."""
    seed(vault, "Old.md", b"moved\n")
    monkeypatch.setattr(tools, "async_session", _no_session())

    out = await tools.move_note_impl(
        "Old.md", "New.md", rewrite_links=True, expected_hash=STALE
    )

    assert "backlink sources" in out
    assert "not bound" in out
    # The sentinel is still the final, line-initial single line.
    assert sentinel(out)["code"] == refusals.STALE_PRECONDITION
    assert out.count(refusals.SENTINEL) == 1


@pytest.mark.parametrize("label, value", MALFORMED)
async def test_move_note_malformed_hash(vault, monkeypatch, label, value):
    seed(vault, "Old.md", b"moved\n")
    monkeypatch.setattr(tools, "async_session", _no_session())

    out = await tools.move_note_impl("Old.md", "New.md", expected_hash=value)

    assert sentinel(out)["code"] == refusals.MALFORMED_PRECONDITION
    assert (vault / "Old.md").exists()
    assert not (vault / "New.md").exists()


async def test_a_plain_move_reports_the_moved_bytes(vault, monkeypatch):
    seed(vault, "Old.md", b"moved\n")
    monkeypatch.setattr(tools, "async_session", _fake_session())

    out = await tools.move_note_impl(
        "Old.md", "New.md", expected_hash=digest(vault, "Old.md")
    )

    assert "Moved Old.md → New.md" in out
    assert reported_hash(out) == digest(vault, "New.md")


def _rewrite_session(vault):
    return _fake_session(
        [_Row(file_path="Old.md", id=1), _Row(file_path="src.md", id=2)],
        [_Row(file_path="src.md")],
    )


SELF_LINKING = b"See [[Old]] for the rest.\n"
BACKLINK = b"Read [[Old]] first.\n"


async def test_a_rewriting_move_reports_the_post_rewrite_destination(
    vault, monkeypatch
):
    """The moved note's own body was rewritten after the rename, so the
    reported hash is the post-rewrite bytes — and it binds the next write."""
    seed(vault, "Old.md", SELF_LINKING)
    seed(vault, "src.md", BACKLINK)
    monkeypatch.setattr(tools, "async_session", _rewrite_session(vault))

    out = await tools.move_note_impl("Old.md", "New.md", rewrite_links=True)

    assert (vault / "New.md").read_bytes() == b"See [[New]] for the rest.\n"
    assert reported_hash(out) == digest(vault, "New.md")

    follow_up = await tools.edit_note_impl(
        "New.md", "next\n", replace_frontmatter=True, expected_hash=reported_hash(out)
    )
    assert not refusals.has_sentinel(follow_up), follow_up


def _fail_writes_to(monkeypatch, names, exc_factory):
    """Make the rewrite of the named files fail, leaving every other write alone."""
    real = tools.write_file_at

    def wrapper(target, content, **kwargs):
        if target.rel in names:
            raise exc_factory(target)
        return real(target, content, **kwargs)

    monkeypatch.setattr(tools, "write_file_at", wrapper)


async def test_a_moved_note_rewrite_failing_without_a_conflict_reports_the_rename(
    vault, monkeypatch
):
    """Nothing observed a change, so the destination still holds exactly what
    the rename published — and that is what may be named."""
    seed(vault, "Old.md", SELF_LINKING)
    seed(vault, "src.md", BACKLINK)
    monkeypatch.setattr(tools, "async_session", _rewrite_session(vault))
    _fail_writes_to(monkeypatch, {"New.md"}, lambda t: OSError("disk on fire"))

    out = await tools.move_note_impl("Old.md", "New.md", rewrite_links=True)

    assert "partial success" in out
    assert (vault / "New.md").read_bytes() == SELF_LINKING
    assert reported_hash(out) == digest(vault, "New.md")
    assert sentinel(out)["code"] == "partial_completion"
    assert out.disposition == "partial"


async def test_a_moved_note_rewrite_losing_the_conflict_reports_no_hash(
    vault, monkeypatch
):
    """Somebody else's bytes are at the destination and this call never read
    them, so no hash the server could name would describe what is on disk."""
    seed(vault, "Old.md", SELF_LINKING)
    seed(vault, "src.md", BACKLINK)
    monkeypatch.setattr(tools, "async_session", _rewrite_session(vault))
    real = tools.write_file_at

    def wrapper(target, content, **kwargs):
        if target.rel == "New.md":
            (vault / "New.md").write_bytes(b"a third writer\n")
        return real(target, content, **kwargs)

    monkeypatch.setattr(tools, "write_file_at", wrapper)

    out = await tools.move_note_impl("Old.md", "New.md", rewrite_links=True)

    payload = sentinel(out)
    assert payload["code"] == refusals.CONCURRENT_WRITE
    # A post-rename failure is a partial success, never "nothing was written":
    # the caller must not go looking for a note that has already relocated.
    assert "nothing_written" not in payload
    assert "content_hash:" not in out
    assert "the move completed" in out
    assert "re-read" in out.lower()
    assert "Moved Old.md → New.md" in out
    assert (vault / "New.md").read_bytes() == b"a third writer\n"


async def test_a_backlink_sources_failure_does_not_change_the_reported_hash(
    vault, monkeypatch
):
    seed(vault, "Old.md", SELF_LINKING)
    seed(vault, "src.md", BACKLINK)
    monkeypatch.setattr(tools, "async_session", _rewrite_session(vault))
    _fail_writes_to(monkeypatch, {"src.md"}, lambda t: OSError("disk on fire"))

    out = await tools.move_note_impl("Old.md", "New.md", rewrite_links=True)

    assert "partial success" in out
    assert "src.md" in out
    # The moved note's own rewrite published, so its hash is what is reported —
    # and no source's hash is reported at all.
    assert (vault / "New.md").read_bytes() == b"See [[New]] for the rest.\n"
    assert reported_hash(out) == digest(vault, "New.md")
    assert out.count("content_hash:") == 1


async def test_an_unguarded_move_reads_no_incumbent(vault, monkeypatch):
    seed(vault, "Old.md", b"moved\n")
    monkeypatch.setattr(tools, "async_session", _fake_session())
    calls = {"n": 0}
    real = tools._read_incumbent

    def counting(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(tools, "_read_incumbent", counting)

    await tools.move_note_impl("Old.md", "New.md")

    assert calls["n"] == 0


# ══════════════════════════════════════════════════════════════════════════
# 7. over-cap incumbents — "I could not check" is not "it differs"
# ══════════════════════════════════════════════════════════════════════════


@pytest.fixture
def tiny_cap(monkeypatch):
    """A note cap small enough that an ordinary fixture note exceeds it."""
    monkeypatch.setattr(tools, "MAX_NOTE_BYTES", 4)


async def _guarded_call(tool: str, vault):
    if tool == "edit_note":
        return await tools.edit_note_impl(
            "n.md", "x", replace_frontmatter=True, expected_hash=STALE
        )
    if tool == "set_frontmatter":
        return await tools.set_frontmatter_impl(
            "n.md", updates={"a": 1}, expected_hash=STALE
        )
    if tool == "delete_note":
        return await tools.delete_note_impl("n.md", expected_hash=STALE)
    return await tools.move_note_impl("n.md", "moved.md", expected_hash=STALE)


@pytest.mark.parametrize(
    "tool", ["edit_note", "set_frontmatter", "delete_note", "move_note"]
)
async def test_an_over_cap_incumbent_is_unavailable_not_stale(
    vault, monkeypatch, tiny_cap, tool
):
    seed(vault, "n.md", b"far more than four bytes\n")
    monkeypatch.setattr(tools, "async_session", _no_session())
    before = (vault / "n.md").read_bytes()

    out = await _guarded_call(tool, vault)

    payload = sentinel(out)
    assert payload["code"] == refusals.PRECONDITION_UNAVAILABLE
    assert payload["cap_name"] == "MAX_NOTE_BYTES"
    assert payload["cap_bytes"] == 4
    assert payload["nothing_written"] is True
    assert "current_hash" not in payload
    assert (vault / "n.md").read_bytes() == before


@pytest.mark.parametrize(
    "tool", ["edit_note", "set_frontmatter", "delete_note", "move_note"]
)
async def test_malformed_beats_unavailable_on_an_over_cap_file(
    vault, monkeypatch, tiny_cap, tool
):
    seed(vault, "n.md", b"far more than four bytes\n")
    monkeypatch.setattr(tools, "async_session", _no_session())
    bad = "0" * 64

    if tool == "edit_note":
        out = await tools.edit_note_impl("n.md", "x", expected_hash=bad)
    elif tool == "set_frontmatter":
        out = await tools.set_frontmatter_impl("n.md", updates={"a": 1}, expected_hash=bad)
    elif tool == "delete_note":
        out = await tools.delete_note_impl("n.md", expected_hash=bad)
    else:
        out = await tools.move_note_impl("n.md", "moved.md", expected_hash=bad)

    assert sentinel(out)["code"] == refusals.MALFORMED_PRECONDITION


async def test_an_unguarded_over_cap_delete_behaves_as_today(vault, tiny_cap):
    """The compatibility rule: nothing that works now stops working because a
    file is too large to hash."""
    seed(vault, "n.md", b"far more than four bytes\n")

    out = await tools.delete_note_impl("n.md", permanent=True)

    assert not refusals.has_sentinel(out), out
    assert not (vault / "n.md").exists()


async def test_an_unguarded_over_cap_move_succeeds_and_reports_no_hash(
    vault, monkeypatch, tiny_cap
):
    seed(vault, "n.md", b"far more than four bytes\n")
    monkeypatch.setattr(tools, "async_session", _fake_session())

    out = await tools.move_note_impl("n.md", "moved.md")

    assert "Moved n.md → moved.md" in out
    assert not refusals.has_sentinel(out), out
    assert "content_hash not reported" in out
    assert "MAX_NOTE_BYTES" in out
    assert (vault / "moved.md").exists()


async def test_an_unguarded_over_cap_edit_keeps_todays_read_error(vault, tiny_cap):
    """`edit_note` cannot proceed without the bytes, so it fails as it does
    today — with its own message, not a precondition refusal."""
    seed(vault, "n.md", b"far more than four bytes\n")

    out = await tools.edit_note_impl("n.md", "x", replace_frontmatter=True)

    assert out.startswith("Failed to read n.md:")
    assert sentinel(out)["code"] == "size_limit"


async def test_required_mode_on_an_over_cap_file_names_the_real_cause(
    vault, monkeypatch, tiny_cap, required_mode
):
    """Telling such a caller to supply a hash sends it after one it can never
    obtain, so `precondition_unavailable` outranks `precondition_required`."""
    seed(vault, "n.md", b"far more than four bytes\n")
    monkeypatch.setattr(tools, "async_session", _no_session())

    out = await tools.delete_note_impl("n.md")

    assert sentinel(out)["code"] == refusals.PRECONDITION_UNAVAILABLE


# ══════════════════════════════════════════════════════════════════════════
# 8. malformed wins over the filesystem, on every tool in this slice
# ══════════════════════════════════════════════════════════════════════════


async def _call_without_hash(tool, path):
    if tool == "create_note":
        return await tools.create_note_impl(path, "body\n")
    if tool == "edit_note":
        return await tools.edit_note_impl(path, "x", replace_frontmatter=True)
    if tool == "set_frontmatter":
        return await tools.set_frontmatter_impl(path, updates={"a": 1})
    if tool == "delete_note":
        return await tools.delete_note_impl(path)
    return await tools.move_note_impl(path, "elsewhere.md")


async def _call_with_hash(tool, path, value):
    if tool == "create_note":
        return await tools.create_note_impl(path, "body\n", expected_hash=value)
    if tool == "edit_note":
        return await tools.edit_note_impl(
            path, "x", replace_frontmatter=True, expected_hash=value
        )
    if tool == "set_frontmatter":
        return await tools.set_frontmatter_impl(
            path, updates={"a": 1}, expected_hash=value
        )
    if tool == "delete_note":
        return await tools.delete_note_impl(path, expected_hash=value)
    return await tools.move_note_impl(path, "elsewhere.md", expected_hash=value)


SLICE_B_TOOLS = [
    "create_note",
    "edit_note",
    "set_frontmatter",
    "delete_note",
    "move_note",
]


@pytest.mark.parametrize("tool", SLICE_B_TOOLS)
async def test_malformed_beats_not_found(vault, monkeypatch, tool):
    """A caller told "not found" for a call whose argument was never valid
    fixes the wrong thing."""
    monkeypatch.setattr(tools, "async_session", _no_session())

    out = await _call_with_hash(tool, "missing.md", "0" * 64)

    assert sentinel(out)["code"] == refusals.MALFORMED_PRECONDITION
    assert "not found" not in out.lower()
    assert not (vault / "missing.md").exists()


@pytest.mark.parametrize("tool", SLICE_B_TOOLS)
async def test_malformed_beats_a_symlinked_leaf(vault, monkeypatch, tool):
    (vault / "real.md").write_bytes(b"real\n")
    os.symlink(str(vault / "real.md"), str(vault / "alias.md"))
    monkeypatch.setattr(tools, "async_session", _no_session())

    # The control: without a hash the symlink is what the caller is told about.
    control = await _call_without_hash(tool, "alias.md")
    assert "symlink" in control.lower() or "link" in control.lower(), control

    out = await _call_with_hash(tool, "alias.md", "sha256:" + "A" * 64)

    assert sentinel(out)["code"] == refusals.MALFORMED_PRECONDITION
    assert (vault / "real.md").read_bytes() == b"real\n"


# ══════════════════════════════════════════════════════════════════════════
# 9. required mode
# ══════════════════════════════════════════════════════════════════════════


async def test_required_mode_refuses_an_unguarded_edit(vault, required_mode):
    name = seed(vault, "n.md", b"body\n")

    out = await tools.edit_note_impl(name, "x", replace_frontmatter=True)

    payload = sentinel(out)
    assert payload["code"] == refusals.PRECONDITION_REQUIRED
    # It had already read the incumbent, so the caller recovers in one retry.
    assert payload["current_hash"] == digest(vault, name)
    assert (vault / name).read_bytes() == b"body\n"


async def test_required_mode_refuses_an_unguarded_set_frontmatter(vault, required_mode):
    name = seed(vault, "n.md", b"---\na: 1\n---\nbody\n")

    out = await tools.set_frontmatter_impl(name, updates={"a": 2})

    assert sentinel(out)["code"] == refusals.PRECONDITION_REQUIRED
    assert b"a: 1" in (vault / name).read_bytes()


@pytest.mark.parametrize("permanent", [False, True])
async def test_required_mode_refuses_an_unguarded_delete(
    vault, required_mode, permanent
):
    name = seed(vault, "n.md", b"body\n")

    out = await tools.delete_note_impl(name, permanent=permanent)

    assert sentinel(out)["code"] == refusals.PRECONDITION_REQUIRED
    assert (vault / name).exists()
    assert not (vault / ".trash").exists()


async def test_required_mode_refuses_an_unguarded_move(vault, monkeypatch, required_mode):
    seed(vault, "Old.md", b"moved\n")
    monkeypatch.setattr(tools, "async_session", _no_session())

    out = await tools.move_note_impl("Old.md", "New.md")

    assert sentinel(out)["code"] == refusals.PRECONDITION_REQUIRED
    assert (vault / "Old.md").exists()
    assert not (vault / "New.md").exists()


async def test_required_mode_lets_a_guarded_write_through(vault, required_mode):
    name = seed(vault, "n.md", b"body\n")

    out = await tools.edit_note_impl(
        name, "new\n", replace_frontmatter=True, expected_hash=digest(vault, name)
    )

    assert not refusals.has_sentinel(out), out
    assert (vault / name).read_bytes() == b"new\n"


# ══════════════════════════════════════════════════════════════════════════
# 10. the audit trail
# ══════════════════════════════════════════════════════════════════════════


@pytest.fixture
def usage_rows(monkeypatch):
    rows: list[dict] = []

    async def recorder(tool, params, duration_ms, response_size):
        rows.append({"tool": tool, "params": params})
        return True

    monkeypatch.setattr(tools, "_log_usage", recorder)
    return rows


async def test_usage_logs_record_the_hash_a_write_was_guarded_with(vault, usage_rows):
    """A digest, not a secret: an operator investigating a lost update needs to
    see which writes were guarded and against which base (design D11)."""
    name = seed(vault, "n.md", b"body\n")
    good = digest(vault, name)

    await tools.edit_note_impl(name, "new\n", replace_frontmatter=True, expected_hash=good)

    assert usage_rows[-1]["tool"] == "edit_note"
    assert usage_rows[-1]["params"]["expected_hash"] == good


async def test_a_refused_write_still_writes_its_usage_row(vault, usage_rows):
    """L10: "nothing was written" is about the vault and the derived index. The
    call's own audit row is exactly what an operator needs to see."""
    name = seed(vault, "n.md", b"body\n")

    await tools.edit_note_impl(
        name, "new\n", replace_frontmatter=True, expected_hash=STALE
    )

    assert usage_rows[-1]["tool"] == "edit_note"
    assert usage_rows[-1]["params"]["expected_hash"] == STALE
    assert (vault / name).read_bytes() == b"body\n"


@pytest.mark.parametrize(
    "tool, params",
    [
        ("create_note", ["path", "expected_hash"]),
        ("delete_note", ["path", "permanent", "expected_hash"]),
        ("set_frontmatter", ["path", "expected_hash"]),
    ],
)
async def test_every_slice_b_tool_logs_the_argument(vault, usage_rows, tool, params):
    seed(vault, "n.md", b"body\n")

    await _call_with_hash(tool, "n.md", STALE)

    assert usage_rows[-1]["tool"] == tool
    for key in params:
        assert key in usage_rows[-1]["params"], usage_rows[-1]
