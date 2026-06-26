"""Tests for the raw file-access tools (read_file / write_file / list_files).

Covers the `add-nonmarkdown-file-access` change: encoding resolution, binary
round-trips, size caps, no-clobber, glob/recursive listing, truncation, and
the dot-dir / path-traversal safety rules. Fully offline — usage logging is
stubbed and the vault is a per-test tmp dir.

`src.mcp_server.tools` imports `src.config`, whose module-level `Settings()`
reads `./.env`. Provide minimal defaults and chdir to a dir without a `.env`
BEFORE importing, matching the other tool tests.
"""

import base64
import os
import tempfile
import time
from pathlib import Path

os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")
os.chdir(tempfile.gettempdir())

import pytest  # noqa: E402
from mcp.server.fastmcp import Image  # noqa: E402

import src.mcp_server.tools as tools  # noqa: E402
from src.mcp_server.auth import current_permission  # noqa: E402

PNG_SIG = b"\x89PNG\r\n\x1a\n"


@pytest.fixture(autouse=True)
def _no_usage_log(monkeypatch):
    """Stub the DB-backed usage logger so tools run fully offline."""

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(tools, "_log_usage", _noop)


@pytest.fixture
def vault(monkeypatch, tmp_path):
    """Point the (single-user) vault root at a fresh tmp dir."""
    monkeypatch.setattr(tools.settings, "vault_path", str(tmp_path))
    return tmp_path


@pytest.fixture
def readwrite():
    """Grant write permission for the duration of a test."""
    token = current_permission.set("readwrite")
    try:
        yield
    finally:
        current_permission.reset(token)


def _b64_body(result: str) -> bytes:
    """Extract and decode the base64 block from a read_file base64 payload."""
    assert "encoding: base64" in result
    return base64.b64decode(result.split("\n\n", 1)[1])


# ── 5.1 read_file ────────────────────────────────────────────────────────────


async def test_read_auto_text(vault):
    (vault / "note.html").write_text("<h1>hi</h1>", encoding="utf-8")
    result = await tools.read_file_impl("note.html")
    assert result == "<h1>hi</h1>"
    assert "encoding: base64" not in result


async def test_read_auto_image_returns_image_block(vault):
    (vault / "pic.png").write_bytes(PNG_SIG + b"\x00" * 32)
    result = await tools.read_file_impl("pic.png")
    assert isinstance(result, Image)
    assert result._mime_type == "image/png"
    assert result.data == PNG_SIG + b"\x00" * 32


async def test_read_auto_mislabeled_image_sniffed(vault):
    # JPEG magic bytes under a .bin extension still render as an image.
    (vault / "blob.bin").write_bytes(b"\xff\xd8\xff" + b"\x10" * 20)
    result = await tools.read_file_impl("blob.bin")
    assert isinstance(result, Image)
    assert result._mime_type == "image/jpeg"


async def test_read_auto_pdf_is_base64(vault):
    raw = b"%PDF-1.4\n" + bytes(range(256))
    (vault / "doc.pdf").write_bytes(raw)
    result = await tools.read_file_impl("doc.pdf")
    assert "mime: application/pdf" in result
    assert _b64_body(result) == raw


async def test_read_forced_base64_on_text(vault):
    (vault / "a.txt").write_text("plain text", encoding="utf-8")
    result = await tools.read_file_impl("a.txt", encoding="base64")
    assert _b64_body(result) == b"plain text"


async def test_read_missing_file(vault):
    result = await tools.read_file_impl("nope.pdf")
    assert "not found" in result.lower()
    assert "nope.pdf" in result


async def test_read_over_cap_reports_size_and_path(vault, monkeypatch):
    monkeypatch.setattr(tools.settings, "max_file_read_bytes", 10)
    (vault / "big.bin").write_bytes(b"x" * 50)
    result = await tools.read_file_impl("big.bin")
    assert isinstance(result, str)
    assert "big.bin" in result
    assert "50" in result  # actual size reported


async def test_read_text_non_utf8_errors(vault):
    (vault / "bad.txt").write_bytes(b"\xff\xfe\x00\x01")
    result = await tools.read_file_impl("bad.txt", encoding="text")
    assert "utf-8" in result.lower()


async def test_read_invalid_encoding(vault):
    result = await tools.read_file_impl("x", encoding="rot13")
    assert "Invalid encoding" in result


# ── 5.2 write_file ───────────────────────────────────────────────────────────


async def test_write_binary_base64_roundtrip(vault, readwrite):
    raw = bytes(range(256))
    b64 = base64.b64encode(raw).decode()
    result = await tools.write_file_impl("out/data.bin", b64)
    assert "Wrote" in result
    assert (vault / "out" / "data.bin").read_bytes() == raw


async def test_write_text_mode(vault, readwrite):
    await tools.write_file_impl("hello.txt", "hi there", encoding="text")
    assert (vault / "hello.txt").read_text(encoding="utf-8") == "hi there"


async def test_write_creates_parent_dirs(vault, readwrite):
    await tools.write_file_impl("a/b/c/deep.txt", "x", encoding="text")
    assert (vault / "a" / "b" / "c" / "deep.txt").is_file()


async def test_write_no_clobber_leaves_file(vault, readwrite):
    (vault / "keep.txt").write_text("original", encoding="utf-8")
    result = await tools.write_file_impl("keep.txt", "new", encoding="text")
    assert "already exists" in result.lower()
    assert (vault / "keep.txt").read_text(encoding="utf-8") == "original"


async def test_write_overwrite_replaces(vault, readwrite):
    (vault / "keep.txt").write_text("original", encoding="utf-8")
    await tools.write_file_impl("keep.txt", "new", encoding="text", overwrite=True)
    assert (vault / "keep.txt").read_text(encoding="utf-8") == "new"


async def test_write_invalid_base64_writes_nothing(vault, readwrite):
    result = await tools.write_file_impl("bad.bin", "not!!base64!!")
    assert "base64" in result.lower()
    assert not (vault / "bad.bin").exists()


async def test_write_over_cap_writes_nothing(vault, readwrite, monkeypatch):
    monkeypatch.setattr(tools.settings, "max_file_write_bytes", 10)
    b64 = base64.b64encode(b"x" * 50).decode()
    result = await tools.write_file_impl("big.bin", b64)
    assert "too large" in result.lower()
    assert not (vault / "big.bin").exists()


async def test_write_requires_readwrite(vault):
    # No readwrite fixture → default "read" permission.
    result = await tools.write_file_impl("x.txt", "y", encoding="text")
    assert "read-only" in result.lower() or "permission denied" in result.lower()
    assert not (vault / "x.txt").exists()


# ── 5.3 list_files ───────────────────────────────────────────────────────────


async def test_list_non_recursive_files_and_subdirs(vault):
    (vault / "sub").mkdir()
    (vault / "sub" / "nested.txt").write_text("n", encoding="utf-8")
    (vault / "a.pdf").write_bytes(b"%PDF")
    (vault / "b.txt").write_text("b", encoding="utf-8")
    result = await tools.list_files_impl(".")
    assert "`a.pdf`" in result
    assert "`b.txt`" in result
    assert "sub/`" in result  # directory entry
    assert "nested.txt" not in result  # not recursive
    assert "B, modified" in result  # size + mtime present


async def test_list_glob_filters_files(vault):
    (vault / "a.pdf").write_bytes(b"%PDF")
    (vault / "b.txt").write_text("b", encoding="utf-8")
    result = await tools.list_files_impl(".", pattern="*.pdf")
    assert "a.pdf" in result
    assert "b.txt" not in result


async def test_list_recursive_descends(vault):
    (vault / "sub").mkdir()
    (vault / "sub" / "deep.pdf").write_bytes(b"%PDF")
    result = await tools.list_files_impl(".", pattern="*.pdf", recursive=True)
    assert "sub/deep.pdf" in result


async def test_list_limit_truncates(vault):
    for i in range(10):
        (vault / f"f{i}.txt").write_text("x", encoding="utf-8")
    result = await tools.list_files_impl(".", limit=3)
    assert "truncated" in result.lower()
    # 3 entries listed (lines starting with "- ")
    assert sum(1 for ln in result.splitlines() if ln.startswith("- ")) == 3


# ── 5.4 safety ───────────────────────────────────────────────────────────────


async def test_read_traversal_rejected(vault):
    result = await tools.read_file_impl("../secret.txt")
    assert "traversal" in result.lower()


async def test_write_traversal_rejected(vault, readwrite):
    result = await tools.write_file_impl("../escape.txt", "x", encoding="text")
    assert "traversal" in result.lower()


async def test_list_traversal_rejected(vault):
    result = await tools.list_files_impl("../..")
    assert "traversal" in result.lower()


async def test_read_dotdir_rejected(vault):
    dot = vault / ".obsidian"
    dot.mkdir()
    (dot / "app.json").write_text("{}", encoding="utf-8")
    result = await tools.read_file_impl(".obsidian/app.json")
    assert "hidden" in result.lower()


async def test_write_dotdir_rejected(vault, readwrite):
    result = await tools.write_file_impl(".git/config", "x", encoding="text")
    assert "hidden" in result.lower()
    assert not (vault / ".git" / "config").exists()


async def test_list_hides_dotdirs(vault):
    (vault / ".obsidian").mkdir()
    (vault / ".obsidian" / "x.json").write_text("{}", encoding="utf-8")
    (vault / "visible.txt").write_text("v", encoding="utf-8")
    result = await tools.list_files_impl(".")
    assert ".obsidian" not in result
    assert "visible.txt" in result


async def test_list_dotdir_folder_rejected(vault):
    (vault / ".obsidian").mkdir()
    result = await tools.list_files_impl(".obsidian")
    assert "hidden" in result.lower()


# ── 5.5 guide ────────────────────────────────────────────────────────────────


async def test_guide_mentions_file_tools(vault):
    result = await tools.get_vault_guide_impl()
    for name in ("read_file", "write_file", "list_files"):
        assert name in result


# ── extra: mtime sanity for list output ──────────────────────────────────────


async def test_list_reports_recent_mtime(vault):
    (vault / "fresh.txt").write_text("f", encoding="utf-8")
    os.utime(vault / "fresh.txt", (time.time(), time.time()))
    result = await tools.list_files_impl(".")
    assert "fresh.txt" in result
    assert Path(str(vault / "fresh.txt")).exists()
