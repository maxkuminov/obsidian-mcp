"""Tool-level caps that stop one tenant stalling the loop — #204, #203, ASVS.

Two holes, both reachable with ordinary authenticated input:

* `list_files` handed `pattern` straight to `fnmatch`, which compiles it with
  `re` at ~10 µs/char on the single event loop. A 500 KB pattern was a 5.4 s
  stall for every other tenant, and the transport body cap admitted about ten
  minutes of one.
* `write_file`, the transfer upload and `import_from_url` are byte transport
  with no extension allowlist, so a 25 MiB `.md` could be landed by a tool the
  note tools would have refused — and the indexer then reads it as a note.
"""

import base64
import os
import tempfile
from contextlib import asynccontextmanager

os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")
os.chdir(tempfile.gettempdir())

import pytest  # noqa: E402

import src.mcp_server.tools as tools  # noqa: E402
from src.config import MAX_LIST_PATTERN_CHARS, MAX_NOTE_BYTES  # noqa: E402
from src.mcp_server.auth import current_permission  # noqa: E402
from src.services import transfer, vault as vault_service, vault_fs  # noqa: E402


@pytest.fixture(autouse=True)
def _no_usage_log(monkeypatch):
    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(tools, "_log_usage", _noop)


@pytest.fixture
def vault(monkeypatch, tmp_path):
    monkeypatch.setattr(tools.settings, "vault_path", str(tmp_path))
    monkeypatch.setattr(tools.settings, "mcp_hostname", "vault.example.com")
    monkeypatch.setattr(tools.settings, "base_url", "https://vault.example.com")
    monkeypatch.setattr(tools.settings, "_public_origin_explicit", True)
    vault_fs.reset_filesystem_probe_cache()
    yield tmp_path
    vault_fs.reset_filesystem_probe_cache()


@pytest.fixture
def readwrite():
    token = current_permission.set("readwrite")
    try:
        yield
    finally:
        current_permission.reset(token)


# ── list_files: the pattern cap ─────────────────────────────────────────────


async def test_a_pattern_at_the_limit_lists_normally(vault):
    (vault / "note.md").write_text("x", encoding="utf-8")
    pattern = "*" + "a" * (MAX_LIST_PATTERN_CHARS - 1)

    result = await tools.list_files_impl(".", pattern=pattern)

    # Matches nothing, but it was compiled and run rather than refused.
    assert "No entries" in result
    assert "MAX_LIST_PATTERN_CHARS" not in result


async def test_a_pattern_one_over_the_limit_is_refused(vault):
    result = await tools.list_files_impl(".", pattern="a" * (MAX_LIST_PATTERN_CHARS + 1))

    assert "MAX_LIST_PATTERN_CHARS" in result
    assert str(MAX_LIST_PATTERN_CHARS) in result.replace(",", "")
    assert "Nothing was listed" in result


async def test_an_over_long_pattern_is_refused_by_its_own_cause(vault):
    """A non-existent folder must still yield the *pattern* error. Told about
    the folder instead, a caller fixes the folder and repeats the stall."""
    result = await tools.list_files_impl(
        "no/such/folder", pattern="a" * (MAX_LIST_PATTERN_CHARS + 1)
    )

    assert "MAX_LIST_PATTERN_CHARS" in result
    assert "Not a directory" not in result


async def test_the_refusal_precedes_path_validation_and_the_compile(
    vault, monkeypatch
):
    """The stall is the compile itself, so nothing may run before the check —
    not the glob compile, and not the path validation that would otherwise
    decide the error."""
    import fnmatch

    def boom(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("ran before the pattern cap")

    monkeypatch.setattr(vault_service, "validate_visible_path", boom)
    monkeypatch.setattr(fnmatch, "fnmatch", boom)
    monkeypatch.setattr(fnmatch, "translate", boom)

    result = await tools.list_files_impl(".", pattern="a" * 500_000)

    assert "MAX_LIST_PATTERN_CHARS" in result


def test_list_dir_raises_value_error_for_an_over_long_pattern(vault):
    """`ValueError` specifically: that is the exception `list_files_impl`
    already maps to an in-band refusal."""
    with pytest.raises(ValueError) as excinfo:
        vault_service.list_dir(".", pattern="a" * (MAX_LIST_PATTERN_CHARS + 1))

    assert "MAX_LIST_PATTERN_CHARS" in str(excinfo.value)


# ── write_file: the markdown cap ────────────────────────────────────────────


async def test_write_file_refuses_an_over_cap_markdown_file(vault, readwrite):
    """The real 10 MiB constant, not a patched one."""
    result = await tools.write_file_impl(
        "big.md", "x" * (MAX_NOTE_BYTES + 1), encoding="text"
    )

    assert "Content too large" in result
    assert "MAX_NOTE_BYTES" in result
    assert str(MAX_NOTE_BYTES) in result.replace(",", "")
    assert not (vault / "big.md").exists()


async def test_write_file_still_accepts_ordinary_markdown(vault, readwrite):
    result = await tools.write_file_impl("ok.md", "x" * 4096, encoding="text")
    assert "Wrote" in result
    assert (vault / "ok.md").stat().st_size == 4096


@pytest.mark.parametrize("name", ["big.MD", "big.Md"])
async def test_the_markdown_cap_is_case_insensitive(vault, readwrite, name):
    result = await tools.write_file_impl(
        name, "x" * (MAX_NOTE_BYTES + 1), encoding="text"
    )
    assert "MAX_NOTE_BYTES" in result
    assert not (vault / name).exists()


async def test_a_large_non_markdown_file_is_unaffected(vault, readwrite):
    """Between the two caps: over `MAX_NOTE_BYTES`, under
    `MAX_FILE_WRITE_BYTES`. The cap follows the extension, not the tool."""
    assert tools.settings.max_file_write_bytes > MAX_NOTE_BYTES
    result = await tools.write_file_impl(
        "big.pdf", "x" * (MAX_NOTE_BYTES + 1), encoding="text"
    )

    assert "Wrote" in result, result
    assert (vault / "big.pdf").stat().st_size == MAX_NOTE_BYTES + 1


async def test_a_lowered_file_cap_binds_markdown_and_is_named(
    vault, readwrite, monkeypatch
):
    """An operator who lowers `MAX_FILE_WRITE_BYTES` below the note cap gets
    the *smaller* of the two for markdown, and is told which one applied."""
    monkeypatch.setattr(tools.settings, "max_file_write_bytes", 100)

    result = await tools.write_file_impl("small.md", "x" * 200, encoding="text")

    assert "Content too large" in result
    assert "MAX_FILE_WRITE_BYTES" in result
    assert "MAX_NOTE_BYTES" not in result
    assert not (vault / "small.md").exists()


async def test_the_non_markdown_refusal_still_names_the_file_cap(
    vault, readwrite, monkeypatch
):
    monkeypatch.setattr(tools.settings, "max_file_write_bytes", 10)
    result = await tools.write_file_impl(
        "big.bin", base64.b64encode(b"x" * 50).decode()
    )

    assert "Content too large" in result
    assert "MAX_FILE_WRITE_BYTES" in result
    assert not (vault / "big.bin").exists()


def test_the_write_cap_helper_returns_the_smaller_limit(monkeypatch):
    monkeypatch.setattr(tools.settings, "max_file_write_bytes", 25 * 1024 * 1024)
    assert tools._write_cap_for("a.md") == (MAX_NOTE_BYTES, "MAX_NOTE_BYTES")
    assert tools._write_cap_for("a.pdf") == (
        25 * 1024 * 1024,
        "MAX_FILE_WRITE_BYTES",
    )
    monkeypatch.setattr(tools.settings, "max_file_write_bytes", 1024)
    assert tools._write_cap_for("a.md") == (1024, "MAX_FILE_WRITE_BYTES")


# ── import_from_url: the same cap on the same extensions ────────────────────


@pytest.fixture
def recording_fetch(monkeypatch):
    """Replace the guarded fetch with a canned stream, recording `max_bytes`."""
    state = {"max_bytes": None, "payload": b"x" * 8}

    @asynccontextmanager
    async def fake_fetch(url, **kwargs):
        state["max_bytes"] = kwargs.get("max_bytes")

        async def body():
            yield state["payload"]

        yield transfer.FetchResult(
            chunks=body(),
            final_url="https://cdn.example.com/a",
            content_type="text/markdown",
        )

    monkeypatch.setattr(transfer, "fetch_url_guarded", fake_fetch)
    return state


async def test_import_passes_the_markdown_cap_to_the_stream(
    vault, readwrite, recording_fetch, monkeypatch
):
    monkeypatch.setattr(tools, "MAX_NOTE_BYTES", 4)
    recording_fetch["payload"] = b"x" * 5

    result = await tools.import_from_url_impl("https://example.com/a.md", "a.md")

    assert recording_fetch["max_bytes"] == 4
    assert "MAX_NOTE_BYTES" in result
    assert "Nothing was written." in result
    assert not (vault / "a.md").exists()


async def test_import_passes_the_file_cap_for_a_non_markdown_target(
    vault, readwrite, recording_fetch, monkeypatch
):
    monkeypatch.setattr(tools.settings, "max_file_write_bytes", 4)
    recording_fetch["payload"] = b"x" * 5

    result = await tools.import_from_url_impl("https://example.com/a.png", "a.png")

    assert recording_fetch["max_bytes"] == 4
    assert "MAX_FILE_WRITE_BYTES" in result
    assert not (vault / "a.png").exists()


async def test_import_of_a_markdown_target_is_capped_case_insensitively(
    vault, readwrite, recording_fetch, monkeypatch
):
    monkeypatch.setattr(tools, "MAX_NOTE_BYTES", 4)
    recording_fetch["payload"] = b"x" * 5

    await tools.import_from_url_impl("https://example.com/a.MD", "a.MD")

    assert recording_fetch["max_bytes"] == 4
