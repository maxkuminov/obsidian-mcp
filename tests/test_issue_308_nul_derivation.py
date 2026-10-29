"""#308 — NUL-free derivation, present-but-not-indexable paths (design D1, D2, D7).

PostgreSQL `text` cannot hold U+0000. One NUL in a note used to fail the
keyword-vector UPDATE and roll the owner's whole index pass back, every tick,
for a week. These are the offline halves: the decode point, the shared title
and tag rules, the JSONB boundary, the link extractor, and the scan's
classification of paths the index can never hold. The real-PostgreSQL halves
are in `tests/integration/test_issue_308_nul_derivation_pg.py`.

Offline: a `tmp_path` vault, no DB.
"""

import hashlib
import logging
import os
import tempfile
import threading

os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")
os.chdir(tempfile.gettempdir())

import pytest  # noqa: E402

import src.mcp_server.tools as tools  # noqa: E402
from src.mcp_server.auth import current_permission  # noqa: E402
from src.services import indexer  # noqa: E402
from src.services import vault as vault_service  # noqa: E402
from src.services.links import extract_links  # noqa: E402
from src.services.vault import MAX_PATH_CHARS, MAX_TAG_BYTES, parse_frontmatter  # noqa: E402


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.fixture(autouse=True)
def _no_usage_log(monkeypatch):
    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(tools, "_log_usage", _noop)


@pytest.fixture(autouse=True)
def _writable():
    token = current_permission.set("readwrite")
    yield
    current_permission.reset(token)


@pytest.fixture
def vault(monkeypatch, tmp_path):
    monkeypatch.setattr(tools.settings, "vault_path", str(tmp_path))
    vault_service.clear_user_vault_cache()
    return tmp_path


def read_at(root, rel):
    with indexer.pinned_root(root) as root_fd:
        return indexer.read_note_beneath(root_fd, rel)


def scan(root, snapshot=None):
    with indexer.pinned_root(root) as root_fd:
        return indexer._scan_vault(
            root_fd,
            snapshot or {},
            force_read=True,
            re_derive=False,
            stop=threading.Event(),
        )


# ── D1: the decode point ──────────────────────────────────────────────────


def test_a_nul_free_note_reads_and_hashes_exactly_as_before(vault):
    body = "---\ntitle: T\n---\nplain body\r\nwith CRLF\n"
    (vault / "a.md").write_bytes(body.encode("utf-8"))
    text, _ = read_at(vault, "a.md")
    assert text == body.replace("\r\n", "\n")
    assert indexer._content_hash(text) == sha(body.replace("\r\n", "\n"))


def test_nul_is_removed_before_hashing_and_the_file_is_untouched(vault, caplog):
    (vault / "dir").mkdir()
    (vault / "dir" / "n.md").write_bytes(b"hello\x00world\x00\n")
    with caplog.at_level(logging.WARNING, logger="src.services.indexer"):
        text, _ = read_at(vault, "dir/n.md")
    assert text == "helloworld\n"
    assert indexer._content_hash(text) == sha("helloworld\n")
    assert (vault / "dir" / "n.md").read_bytes() == b"hello\x00world\x00\n"
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "dir/n.md" in warnings[0] and "2 NUL" in warnings[0]
    assert "hello" not in warnings[0], "never log note content"


def test_the_scan_and_the_re_verifying_readers_agree_on_the_hash(vault):
    """Every reader shares the decode point, so no pair can disagree (D1)."""
    (vault / "n.md").write_bytes(b"a\x00b\n")
    result = scan(vault)
    with indexer.pinned_root(vault) as root_fd:
        _frontmatter, body, h = indexer._read_body_and_hash(root_fd, "n.md")
        rescanned = indexer._rescan_one(root_fd, "n.md")
    assert result.files["n.md"].content_hash == sha("ab\n") == h == rescanned.content_hash
    assert body == "ab\n" and "\x00" not in rescanned.raw


# ── D2: title, tags, JSONB ────────────────────────────────────────────────


def test_the_title_rule_removes_nul():
    assert vault_service.note_title({"title": "a\x00b"}, "n.md") == "ab"
    assert indexer._note_title({"title": "a\x00b"}, "n.md") == "ab"
    # Nothing but NUL: the stem, as any other empty title.
    assert vault_service.note_title({"title": "\x00\x00"}, "Stem.md") == "Stem"
    # NUL-free titles are unchanged, falsy fallback included.
    assert vault_service.note_title({"title": "Plain"}, "n.md") == "Plain"
    assert vault_service.note_title({"title": 0}, "Stem.md") == "Stem"
    assert vault_service.note_title({"title": ["a", "b"]}, "n.md") == "['a', 'b']"


def test_the_tag_rule_removes_nul_without_touching_the_mapping():
    fm = {"tags": ["x\x00y", "plain"]}
    assert vault_service.extract_tags("body #inline\n", fm) == ["inline", "plain", "xy"]
    assert fm == {"tags": ["x\x00y", "plain"]}, "the parsed mapping is not altered"
    assert vault_service.extract_tags("", {"tags": "a\x00, b"}) == ["a", "b"]


def test_an_over_long_tag_is_dropped_and_its_size_reported(caplog):
    long_tag = "t" * 3000
    edge = "e" * MAX_TAG_BYTES
    multibyte = "é" * (MAX_TAG_BYTES // 2 + 1)  # 1,026 bytes, 513 characters
    dropped: list[int] = []
    with caplog.at_level(logging.WARNING, logger="src.services.vault"):
        tags = vault_service.extract_tags(
            "", {"tags": [long_tag, edge, multibyte, "ok"]}, dropped=dropped
        )
    assert tags == sorted([edge, "ok"])
    assert sorted(dropped) == [1026, 3000]
    # The shared (request-path) helper never logs; the indexer does.
    assert caplog.records == []
    assert vault_service.extract_tags("", {"tags": [long_tag]}) == []


@pytest.mark.asyncio
async def test_the_indexer_warns_about_a_dropped_tag_naming_the_note(
    monkeypatch, tmp_path, caplog
):
    from tests.test_perf_stat_shortcut import FakeIndexDB, install

    (tmp_path / "big.md").write_text(
        f"---\ntags: [{'t' * 3000}, ok]\n---\nbody\n", encoding="utf-8"
    )
    db = FakeIndexDB()
    install(monkeypatch, db, tmp_path)
    with caplog.at_level(logging.WARNING, logger="src.services.indexer"):
        await indexer.index_vault()
    messages = [r.getMessage() for r in caplog.records if "tag(s)" in r.getMessage()]
    assert len(messages) == 1
    assert "big.md" in messages[0] and "3000" in messages[0]
    assert "ttt" not in messages[0]


def test_the_jsonb_boundary_removes_nul_from_keys_and_values():
    fm = {"k\x00": 1, "title": "a\x00b", "nested": {"x": ["p\x00q", {"y\x00": "z\x00"}]}}
    assert indexer._sanitize_frontmatter(fm) == {
        "k": 1,
        "title": "ab",
        "nested": {"x": ["pq", {"y": "z"}]},
    }
    # The mapping itself is untouched.
    assert "k\x00" in fm


def test_a_key_that_collides_after_nul_removal_keeps_the_first():
    assert indexer._sanitize_frontmatter({"k\x00": 1, "k": 2}) == {"k": 1}
    assert indexer._sanitize_frontmatter({"k": 2, "k\x00": 1}) == {"k": 2}


def test_read_note_metadata_agrees_with_the_indexer_on_a_yaml_escaped_nul(vault):
    raw = '---\ntitle: "a\\0b"\ntags: ["x\\0y"]\n"k\\0": 1\n---\nbody\n'
    (vault / "n.md").write_text(raw, encoding="utf-8")
    data = vault_service.read_file("n.md")
    assert data["title"] == "ab"
    assert "xy" in data["tags"]
    # The parsed mapping `read_note` returns, and `set_frontmatter`
    # re-serialises, still holds the escaped NUL: only derivations change.
    assert data["frontmatter"]["title"] == "a\x00b"
    assert data["title"] == indexer._note_title(data["frontmatter"], "n.md")


@pytest.mark.asyncio
async def test_set_frontmatter_round_trips_a_yaml_escaped_nul(vault):
    raw = '---\ntitle: "a\\0b"\n---\nbody\n'
    (vault / "n.md").write_text(raw, encoding="utf-8")
    out = await tools.set_frontmatter_impl("n.md", updates={"status": "done"})
    assert "Error" not in out, out
    fm, body = parse_frontmatter((vault / "n.md").read_text(encoding="utf-8"))
    assert fm["title"] == "a\x00b", "set_frontmatter must not strip the NUL"
    assert fm["status"] == "done"
    assert body == "body\n"


# ── D2: links ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "body",
    [
        "[x](bad%00target.md)",
        "[x](<bad%00target.md>)",
        "[x\x00y](target.md)",
        "[[bad\x00target]]",
        "[[target#sec\x00tion]]",
        "[[target|al\x00ias]]",
        "![[bad\x00target]]",
    ],
)
def test_a_link_with_a_nul_is_not_a_link(body):
    assert extract_links(body + "\n") == []


def test_ordinary_links_beside_a_nul_link_are_kept():
    links = extract_links("[x](bad%00target.md) [y](good%20one.md) [[Other|al]]\n")
    assert sorted(link.target for link in links) == ["Other", "good one"]


# ── D7: present but not indexable, at the scan ────────────────────────────


def test_path_indexability():
    assert indexer._path_not_indexable_reason("a/b.md") is None
    assert indexer._path_not_indexable_reason("x" * (MAX_PATH_CHARS - 3) + ".md") is None
    assert "valid UTF-8" in indexer._path_not_indexable_reason("caf\udce9.md")
    assert str(MAX_PATH_CHARS) in indexer._path_not_indexable_reason(
        "x" * MAX_PATH_CHARS + ".md"
    ).replace(",", "")


def test_the_loggable_path_never_raises():
    rendered = indexer._loggable_path("caf\udce9.md")
    rendered.encode("utf-8")
    assert "udce9" in rendered


def _latin1_name(vault) -> str:
    raw = os.path.join(os.fsencode(str(vault)), b"caf\xe9.md")
    try:
        with open(raw, "wb") as fh:
            fh.write(b"latin-1 name\n")
    except OSError as e:  # pragma: no cover - filesystem-dependent
        pytest.skip(f"this filesystem refuses a non-UTF-8 filename: {e}")
    return os.fsdecode(b"caf\xe9.md")


def _long_path(vault) -> str:
    parts = ["d" * 250] * 4 + ["n" * 30 + ".md"]
    directory = vault.joinpath(*parts[:-1])
    directory.mkdir(parents=True)
    (directory / parts[-1]).write_text("deep\n", encoding="utf-8")
    rel = "/".join(parts)
    assert len(rel) > MAX_PATH_CHARS
    return rel


def test_the_scan_classifies_unstorable_paths_and_content_as_not_indexable(
    vault, caplog
):
    (vault / "ok.md").write_text("fine\n", encoding="utf-8")
    (vault / "latin1-body.md").write_bytes(b"caf\xe9\n")
    name = _latin1_name(vault)
    long_rel = _long_path(vault)

    with caplog.at_level(logging.WARNING, logger="src.services.indexer"):
        result = scan(vault)

    assert set(result.files) == {"ok.md"}
    assert set(result.not_indexable) == {name, long_rel, "latin1-body.md"}
    assert result.not_indexable["latin1-body.md"] == indexer.NOT_UTF8_CONTENT
    # Not a skip, not unverified: nothing withholds a stamp or the backstop.
    assert result.skips == [] and result.unverified == [] and result.read_failures == []
    # Every not-indexable path was seen (so it is not pruned as vanished).
    assert {name, long_rel, "latin1-body.md"} <= result.seen
    # The warning renders the path so it can always be logged.
    text = caplog.text
    assert "caf\\xe9.md" in text or "caf\\udce9.md" in text
    for record in caplog.records:
        record.getMessage().encode("utf-8")


def test_a_read_failure_is_a_skip_not_not_indexable(vault, monkeypatch):
    (vault / "a.md").write_text("a\n", encoding="utf-8")
    (vault / "b.md").write_text("b\n", encoding="utf-8")
    real = indexer.read_note_at

    def failing(parent_fd, name, rel=None):
        if name == "b.md":
            raise PermissionError(13, "denied", name)
        return real(parent_fd, name, rel)

    monkeypatch.setattr(indexer, "read_note_at", failing)
    result = scan(vault)
    assert set(result.files) == {"a.md"}
    assert result.not_indexable == {}
    assert [rel for rel, _ in result.read_failures] == ["b.md"]
    assert len(result.unverified) == 1 and len(result.skips) == 1


def test_the_result_is_still_a_two_tuple():
    r = indexer.IndexPassResult(3, 1, rederive=indexer.REDERIVE_INCOMPLETE)
    scanned, indexed = r
    assert (scanned, indexed) == (3, 1) == r
    assert r.rederive_incomplete and r.notes_scanned == 3 and r.notes_indexed == 1
    assert not indexer.IndexPassResult(3, 1).rederive_incomplete
    stats = indexer.PassStats()
    stats.record_index(r)
    assert (stats.notes_scanned, stats.notes_indexed) == (3, 1)
