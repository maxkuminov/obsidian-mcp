"""The write-precondition digest, its refusal ladder, and the read surface (#205).

Slice A of `write-preconditions`. The failure class the whole change exists to
close is **lost update from stale client state**: an agent reads a note, spends
a turn thinking, and writes back a result computed from bytes that are no
longer on disk. The server's existing `expected=` compare cannot see that — it
compares the bytes *this call* read — so the caller needs a token for the bytes
*it* read, which is what `content_hash` is.

What is pinned here, all of it offline (a `tmp_path` vault, `_log_usage`
stubbed, no database):

  * **the digest** — `sha256:` + lowercase hex over the file's complete raw
    bytes, for LF, CRLF, lone-CR and frontmatter-bearing notes; not a hash of
    the `content` the caller received; not `notes_metadata.content_hash`;
  * **one read** — `read_file` reads the note's bytes once, and the text it
    derives from them is byte-identical to what `Path.read_text(encoding=
    "utf-8")` returned before, across all three terminator dialects and with a
    BOM;
  * **the read surface** — the same whole-file hash on a whole-note read, a
    section read and a truncated read, and never dropped under budget pressure;
  * **the canonical input form** — only `sha256:<64 lowercase hex>`, so that
    the index column's bare-hex value is refused as malformed rather than
    reported as a conflict a caller cannot diagnose;
  * **the precedence ladder**, exercised directly against the two helpers —
    malformed beats no-incumbent beats unavailable beats required beats the
    comparison — including that the syntax half needs no path and no
    descriptor, which is what lets it run at a tool's entry;
  * **the sentinel** — one final line, the cap named by name and value on
    `precondition_unavailable`, absent fields absent, and the line surviving a
    long multi-line prose half.
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
from src.mcp_server.auth import current_permission  # noqa: E402
from src.mcp_server.read_result import ReadNoteResult  # noqa: E402
from src.services import refusals  # noqa: E402
from src.services import vault as vault_service  # noqa: E402
from src.services.indexer import _content_hash as index_content_hash  # noqa: E402


# ── fixtures ────────────────────────────────────────────────────────────────


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


def write_bytes(vault, name, data: bytes) -> str:
    (vault / name).write_bytes(data)
    return name


def digest_of(vault, name) -> str:
    """The digest as a caller would compute it from the file itself."""
    return "sha256:" + hashlib.sha256((vault / name).read_bytes()).hexdigest()


def sentinel(text: str) -> dict:
    """The refusal's machine-readable final line, parsed."""
    last = text.rsplit("\n", 1)[-1]
    assert last.startswith(f"{refusals.SENTINEL} "), text
    return json.loads(last[len(refusals.SENTINEL) + 1 :])


# ══════════════════════════════════════════════════════════════════════════
# 1. the digest is the file's raw bytes, and nothing else
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "name, data",
    [
        ("lf.md", b"# A\nbody\n"),
        ("crlf.md", b"# A\r\nbody\r\n"),
        ("cr.md", b"# A\rbody\r"),
        ("fm.md", b"---\ntitle: T\n---\n# A\nbody\n"),
        ("fm-crlf.md", b"---\r\ntitle: T\r\n---\r\n# A\r\nbody\r\n"),
        ("empty.md", b""),
        ("bom.md", b"\xef\xbb\xbf# A\nbody\n"),
    ],
)
@pytest.mark.asyncio
async def test_the_digest_is_sha256_of_the_files_raw_bytes(vault, name, data):
    """No translation, no stripping, no re-encoding — the bytes on disk.

    The CRLF and lone-CR rows are the ones that matter: `read_note`'s own
    `content` has had its terminators translated, so a digest computed over the
    response could never bind the file.
    """
    write_bytes(vault, name, data)
    out = await tools.read_note_impl(name)
    assert out.error is None
    assert out.content_hash == digest_of(vault, name)
    assert out.content_hash.startswith("sha256:")
    assert len(out.content_hash) == vault_service.CONTENT_HASH_CHARS
    assert out.content_hash == out.content_hash.lower()


@pytest.mark.asyncio
async def test_the_digest_is_not_a_hash_of_the_content_field(vault):
    """The field is a token to hand back, not a checksum of what you received.

    Two independent reasons it cannot be: the frontmatter block is not in
    `content`, and the terminators were translated on the way out.
    """
    name = write_bytes(vault, "n.md", b"---\ntitle: T\n---\n# A\r\nbody\r\n")
    out = await tools.read_note_impl(name)
    of_content = "sha256:" + hashlib.sha256(out.content.encode("utf-8")).hexdigest()
    assert out.content_hash != of_content
    assert out.content_hash == digest_of(vault, name)


@pytest.mark.asyncio
async def test_the_digest_is_not_the_index_columns_hash(vault):
    """`notes_metadata.content_hash` hashes *translated text* on purpose.

    Same algorithm, different input, so for any note whose terminators are not
    LF the two differ — which is exactly why handing the column's value to a
    write tool is a malformed argument (below) rather than a conflict report.
    """
    name = write_bytes(vault, "crlf.md", b"# A\r\nbody\r\n")
    out = await tools.read_note_impl(name)
    column = index_content_hash((vault / name).read_text(encoding="utf-8"))
    assert out.content_hash != column
    assert out.content_hash != f"sha256:{column}"
    # …and for an LF note the *digests* coincide while the forms do not, so a
    # caller cannot get away with sending the column's value even there.
    lf = write_bytes(vault, "lf.md", b"# A\nbody\n")
    lf_out = await tools.read_note_impl(lf)
    lf_column = index_content_hash((vault / lf).read_text(encoding="utf-8"))
    assert lf_out.content_hash == f"sha256:{lf_column}"
    assert lf_out.content_hash != lf_column


# ══════════════════════════════════════════════════════════════════════════
# 2. one read serves the text and the hash
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "data",
    [
        b"# A\nbody\n",
        b"# A\r\nbody\r\n",
        b"# A\rbody\r",
        b"---\ntitle: T\n---\nmixed\r\nline\rends\n",
        b"\xef\xbb\xbf# A\nbody\n",
        b"",
        b"no terminator",
    ],
)
def test_read_file_text_is_byte_identical_to_read_text(vault, data):
    """`read_file` reads bytes once and translates them itself.

    The explicit translation has to reproduce `Path.read_text(encoding="utf-8")`
    exactly — CRLF, lone CR, a BOM (which `utf-8`, unlike `utf-8-sig`, keeps as
    a character) — or the bytes-once change would silently alter every read.
    """
    name = write_bytes(vault, "n.md", data)
    expected = (vault / name).read_text(encoding="utf-8")
    assert vault_service._decode_note_bytes(data) == expected
    note = vault_service.read_file(name)
    # The note's own text, reassembled from the fields the read returns.
    if note["frontmatter_yaml"] is None:
        assert note["content"] == expected
    assert note["content_hash"] == digest_of(vault, name)


def test_read_file_does_not_open_the_note_twice(vault, monkeypatch):
    """#149's D3, applied to the hash: two reads of one note can disagree, and
    a hash that describes different bytes than the response is worse than no
    hash. The read is counted, not assumed."""
    name = write_bytes(vault, "n.md", b"# A\nbody\n")
    # Computed before the patch: `Path.read_bytes` opens the file too.
    expected = digest_of(vault, name)
    opens = []
    real_open = vault_service.Path.open

    def counting_open(self, *a, **kw):
        opens.append(str(self))
        return real_open(self, *a, **kw)

    monkeypatch.setattr(vault_service.Path, "open", counting_open)
    note = vault_service.read_file(name)
    assert note["content_hash"] == expected
    assert [p for p in opens if p.endswith("n.md")] == [str(vault / name)]


# ══════════════════════════════════════════════════════════════════════════
# 3. the read surface: same hash in every mode, never dropped
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_section_and_truncated_reads_carry_the_whole_files_hash(
    vault, monkeypatch
):
    """One token, whatever the read selected.

    A section hash would be unsound — `#N` ordinals are positional, so a body
    digest would certify an unchanged body while an insertion above it changed
    which section the selector names — so every mode returns the whole file's.
    """
    name = write_bytes(
        vault, "n.md", b"---\ntitle: T\n---\n# A\n" + b"a" * 4_000 + b"\n## B\nb\n"
    )
    whole = await tools.read_note_impl(name)
    section = await tools.read_note_impl(name, section="B")
    monkeypatch.setattr(tools.settings, "max_read_response_chars", 500)
    truncated = await tools.read_note_impl(name)
    windowed = await tools.read_note_impl(name, offset=100, limit=50)

    assert truncated.truncated is True
    assert section.heading == "## B"
    for out in (whole, section, truncated, windowed):
        assert out.content_hash == digest_of(vault, name)


@pytest.mark.asyncio
async def test_the_hash_survives_a_metadata_budget_drop_of_everything_else(
    vault, monkeypatch
):
    """Dropping it would disable the precondition on exactly the notes big
    enough to be worth guarding, so it is a fixed allocation beside `path`,
    not a budgeted field."""
    monkeypatch.setattr(tools.settings, "max_read_response_chars", 500)
    name = write_bytes(
        vault,
        "n.md",
        b'---\ntitle: "' + b"t" * 400 + b'"\ntags: ['
        # Distinct entries: `extract_tags` returns a set, so identical ones
        # collapse and the metadata budget is never actually loaded.
        + b", ".join(b"g" * 36 + b"%04d" % i for i in range(50))
        + b"]\nfiller: " + b"f" * 5_000
        + b"\n---\nbody\n",
    )
    out = await tools.read_note_impl(name)
    dropped = {o.field for o in out.metadata_omissions}
    assert {"frontmatter", "frontmatter_yaml", "tags"} <= dropped
    assert out.content_hash == digest_of(vault, name)
    assert "content_hash" not in dropped
    assert "path" not in dropped


@pytest.mark.asyncio
async def test_an_error_result_carries_no_hash(vault):
    out = await tools.read_note_impl("nope.md")
    assert out.error is not None
    assert out.content_hash is None
    assert "content_hash" not in out.model_dump()


def test_the_field_is_absent_not_null_when_unset():
    assert "content_hash" not in ReadNoteResult(error="x").model_dump()
    assert "metadata_coercions" not in ReadNoteResult(error="x").model_dump()


# ══════════════════════════════════════════════════════════════════════════
# 4. the canonical input form
# ══════════════════════════════════════════════════════════════════════════


CANONICAL = "sha256:" + "a" * 64


@pytest.mark.parametrize(
    "value, ok",
    [
        (CANONICAL, True),
        ("sha256:" + "0123456789abcdef" * 4, True),
        ("a" * 64, False),  # bare hex — the index column's own shape
        ("sha256:" + "A" * 64, False),  # uppercase
        ("SHA256:" + "a" * 64, False),  # uppercase prefix
        (" " + CANONICAL, False),  # leading whitespace
        (CANONICAL + " ", False),  # trailing whitespace
        (CANONICAL + "\n", False),  # a trailing newline is whitespace too
        ("sha256:" + "a" * 63, False),  # short
        ("sha256:" + "a" * 65, False),  # long
        ("sha512:" + "a" * 64, False),  # another algorithm
        ("sha256:" + "g" * 64, False),  # not hex
        ("sha256:", False),
        ("", False),
        (None, False),
        (12, False),
    ],
)
def test_only_the_canonical_form_is_accepted(value, ok):
    assert vault_service.is_canonical_content_hash(value) is ok


def test_the_digest_helper_and_the_parser_agree():
    """Whatever the digest function emits is a form the parser accepts —
    otherwise a caller handing back exactly what it was given is refused."""
    for data in (b"", b"x", b"\xff\xfe", b"# A\r\n"):
        assert vault_service.is_canonical_content_hash(
            vault_service.content_hash_for_bytes(data)
        )


# ══════════════════════════════════════════════════════════════════════════
# 5. the precedence ladder, against the helpers themselves
# ══════════════════════════════════════════════════════════════════════════
#
# Exercised here rather than only through a tool, because the ladder is the
# design: each step outranks the next, and the combinations are what make that
# observable. Slices B and C then assert the same order through every tool.


BODY = b"# A\nbody\n"
BODY_HASH = vault_service.content_hash_for_bytes(BODY)
STALE = "sha256:" + "b" * 64


def code_of(message: str) -> str:
    return sentinel(message)["code"]


def test_the_syntax_helper_needs_no_path_and_no_descriptor():
    """A pure function of the argument — which is what lets it run at a tool's
    entry, ahead of path resolution, the leaf check and every read."""
    assert tools._precondition_syntax_error("edit_note", None) is None
    assert tools._precondition_syntax_error("edit_note", BODY_HASH) is None
    refused = tools._precondition_syntax_error("edit_note", "a" * 64)
    assert code_of(refused) == refusals.MALFORMED_PRECONDITION
    assert sentinel(refused).get("path") is None
    assert "sha256:<64 lowercase hex>" in refused


def test_malformed_beats_no_incumbent():
    """`create_note` with a bare-hex hash learns it sent the wrong kind of
    value, not something about a file its argument never validly named."""
    out = tools._precondition_error(
        "create_note", "n.md", None, "a" * 64, no_incumbent=True
    )
    assert code_of(out) == refusals.MALFORMED_PRECONDITION


def test_malformed_beats_unavailable_and_required(monkeypatch):
    monkeypatch.setattr(tools.settings, "write_precondition_required", True)
    out = tools._precondition_error(
        "write_file",
        "big.bin",
        None,
        "sha256:" + "A" * 64,
        cap_name="MAX_FILE_READ_BYTES",
        cap_bytes=10,
        over_cap=True,
    )
    assert code_of(out) == refusals.MALFORMED_PRECONDITION


def test_no_incumbent_beats_unavailable_and_the_comparison():
    out = tools._precondition_error(
        "write_file", "n.bin", None, STALE, no_incumbent=True, over_cap=True
    )
    payload = sentinel(out)
    assert payload["code"] == refusals.NO_INCUMBENT
    assert payload["path"] == "n.bin"
    assert payload["nothing_written"] is True
    assert "current_hash" not in payload
    assert "without expected_hash" in out


def test_unavailable_beats_required(monkeypatch):
    """Telling a caller to supply a hash it can never obtain sends it into a
    loop that cannot end, so "I could not check" outranks "you sent none"."""
    monkeypatch.setattr(tools.settings, "write_precondition_required", True)
    out = tools._precondition_error(
        "edit_note",
        "big.md",
        None,
        None,
        cap_name="MAX_NOTE_BYTES",
        cap_bytes=1_048_576,
        over_cap=True,
    )
    payload = sentinel(out)
    assert payload["code"] == refusals.PRECONDITION_UNAVAILABLE
    assert payload["cap_name"] == "MAX_NOTE_BYTES"
    assert payload["cap_bytes"] == 1_048_576
    assert "MAX_NOTE_BYTES" in out and "1,048,576" in out
    assert "operator" in out


def test_an_over_cap_file_is_no_refusal_at_all_when_unguarded(monkeypatch):
    """The compatibility rule: nothing that succeeds today may start failing
    because a file is too large to hash."""
    monkeypatch.setattr(tools.settings, "write_precondition_required", False)
    assert (
        tools._precondition_error(
            "write_file",
            "big.bin",
            None,
            None,
            cap_name="MAX_FILE_READ_BYTES",
            cap_bytes=10,
            over_cap=True,
        )
        is None
    )


def test_required_beats_the_comparison_and_names_the_current_hash(monkeypatch):
    monkeypatch.setattr(tools.settings, "write_precondition_required", True)
    out = tools._precondition_error("edit_note", "n.md", BODY, None)
    payload = sentinel(out)
    assert payload["code"] == refusals.PRECONDITION_REQUIRED
    assert payload["current_hash"] == BODY_HASH
    assert payload["nothing_written"] is True
    assert "WRITE_PRECONDITION_REQUIRED" in out


def test_required_mode_exempts_a_tool_with_no_incumbent(monkeypatch):
    """Requiring a hash where none can exist would make creation impossible."""
    monkeypatch.setattr(tools.settings, "write_precondition_required", True)
    assert (
        tools._precondition_error("create_note", "n.md", None, None, no_incumbent=True)
        is None
    )
    assert (
        tools._precondition_error(
            "edit_note", "n.md", BODY, None, enforceable=False
        )
        is None
    )


def test_the_comparison_matches_and_differs():
    assert tools._precondition_error("edit_note", "n.md", BODY, BODY_HASH) is None
    out = tools._precondition_error("edit_note", "n.md", BODY, STALE)
    payload = sentinel(out)
    assert payload["code"] == refusals.STALE_PRECONDITION
    assert payload["current_hash"] == BODY_HASH
    assert payload["path"] == "n.md"
    assert payload["nothing_written"] is True
    assert "read_note" in out


def test_an_omitted_hash_is_no_refusal_by_default(monkeypatch):
    monkeypatch.setattr(tools.settings, "write_precondition_required", False)
    assert tools._precondition_error("edit_note", "n.md", BODY, None) is None


def test_a_guard_wired_without_bytes_or_a_reason_fails_loudly():
    """A helper that quietly admitted every write would be worse than no
    helper: the wiring error has to be caught, not tolerated."""
    with pytest.raises(ValueError):
        tools._precondition_error("edit_note", "n.md", None, BODY_HASH)


def test_no_precondition_refusal_carries_a_retry_delay():
    """No interval makes a stale hash match, a malformed one canonical, or a
    file smaller than a cap."""
    for code in refusals.PRECONDITION_CODES:
        with pytest.raises(refusals.RefusalShapeError):
            refusals.Refusal(code=code, retry_after_seconds=1)


# ── the bounded incumbent read ──────────────────────────────────────────────


def test_the_incumbent_read_is_bounded_by_the_tools_own_cap(vault):
    name = write_bytes(vault, "n.md", BODY)
    with vault_service.open_mutable(name) as target:
        data, over = tools._read_incumbent(target, name, 1_000)
        assert (data, over) == (BODY, False)
        data, over = tools._read_incumbent(target, name, 3)
        assert data is None and over is True


def test_a_missing_incumbent_is_not_an_over_cap_one(vault):
    with vault_service.open_mutable("gone.md") as target:
        assert tools._read_incumbent(target, "gone.md", 1_000) == (None, False)


def test_the_caps_are_named_as_well_as_valued():
    assert tools._note_precondition_cap() == ("MAX_NOTE_BYTES", tools.MAX_NOTE_BYTES)
    assert tools._file_precondition_cap() == (
        "MAX_FILE_READ_BYTES",
        tools.settings.max_file_read_bytes,
    )


# ══════════════════════════════════════════════════════════════════════════
# 6. the sentinel line
# ══════════════════════════════════════════════════════════════════════════


def test_the_sentinel_is_one_final_line_after_a_long_prose_half():
    out = tools._precondition_error("edit_note", "n.md", BODY, STALE)
    assert out.count(f"\n{refusals.SENTINEL} ") == 1
    assert out.count(refusals.SENTINEL) == 1
    prose, line = out.rsplit("\n", 1)
    assert len(prose) > 100
    assert "\n" not in line
    assert json.loads(line[len(refusals.SENTINEL) + 1 :])["code"] == (
        refusals.STALE_PRECONDITION
    )


def test_a_precondition_line_omits_every_field_that_does_not_apply():
    """Absent, never null: `"current_hash":null` invites a client to read a
    field that means nothing, and the bucket vocabulary says nothing about a
    file."""
    payload = sentinel(
        tools._precondition_error(
            "create_note", "n.md", None, CANONICAL, no_incumbent=True
        )
    )
    assert payload == {
        "code": refusals.NO_INCUMBENT,
        "path": "n.md",
        "nothing_written": True,
    }


def test_the_gate_refusals_keep_their_own_five_field_shape():
    """The precondition codes are additive: the sibling rate/quota vocabulary
    renders exactly as it did."""
    payload = sentinel(
        refusals.render("prose", refusals.Refusal(code=refusals.OVER_QUOTA))
    )
    assert payload == {
        "code": refusals.OVER_QUOTA,
        "scope": None,
        "limit": None,
        "limit_unit": None,
    }


def test_a_cap_is_a_name_and_a_value_or_it_is_neither():
    with pytest.raises(refusals.RefusalShapeError):
        refusals.Refusal(code=refusals.PRECONDITION_UNAVAILABLE, path="n.md")
    with pytest.raises(refusals.RefusalShapeError):
        refusals.Refusal(code=refusals.STALE_PRECONDITION, cap_bytes=10)


def test_the_in_call_conflict_keeps_its_prose_and_gains_the_code():
    """Two windows, two codes — and the older refusal's wording is untouched,
    so every existing `in` / `startswith` assertion still holds."""
    prose = "File changed while editing: n.md"
    out = tools._concurrent_write_refusal(prose, "n.md")
    assert out.startswith(prose)
    payload = sentinel(out)
    assert payload["code"] == refusals.CONCURRENT_WRITE
    assert payload["nothing_written"] is True
    assert "current_hash" not in payload
    # A post-rename partial success may render the same code while saying
    # nothing about whether the vault changed — because it did.
    partial = tools._concurrent_write_refusal(prose, "n.md", nothing_written=None)
    assert "nothing_written" not in sentinel(partial)


def test_the_two_windows_are_distinguishable_by_code():
    stale = tools._precondition_error("edit_note", "n.md", BODY, STALE)
    in_call = tools._concurrent_write_refusal("File changed while editing: n.md")
    assert code_of(stale) != code_of(in_call)
    assert sentinel(stale)["current_hash"] == BODY_HASH
    assert "current_hash" not in sentinel(in_call)


# ══════════════════════════════════════════════════════════════════════════
# 7. one title rule, adopted by the read path (design D10b)
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "block, expected",
    [
        ("title: Plain string", "Plain string"),
        ("title: 2026-08-25", "2026-08-25"),
        ("title: [2026-08-25]", "['2026-08-25']"),
        ("title:\n  1: a", "{'1': 'a'}"),
        ("title: [a, b]", "['a', 'b']"),
        ("title: 5", "5"),
        ("title: .nan", ".nan"),
        ("title: .INF", ".inf"),
        ("title: -.inf", "-.inf"),
    ],
)
def test_the_read_path_titles_a_note_the_way_the_index_does(vault, block, expected):
    """The indexer's rule is the rule (D10b): it is already what search
    results and every listing show, and it is bounded to the column's width.
    Three of these rows are declared changes to `read_note` (L13)."""
    name = write_bytes(vault, "n.md", f"---\n{block}\n---\nbody\n".encode())
    assert vault_service.read_file(name)["title"] == expected


@pytest.mark.parametrize("falsy", ["0", "false", '""', "[]"])
def test_a_falsy_title_falls_back_to_the_filename_stem(vault, falsy):
    name = write_bytes(vault, "n.md", f"---\ntitle: {falsy}\n---\nbody\n".encode())
    assert vault_service.read_file(name)["title"] == "n"


def test_a_title_is_bounded_to_the_column_width(vault):
    name = write_bytes(vault, "n.md", b"---\ntitle: " + b"t" * 600 + b"\n---\nb\n")
    assert vault_service.read_file(name)["title"] == "t" * 512
    assert vault_service.TITLE_MAX_CHARS == 512


@pytest.mark.parametrize(
    "value, token",
    [
        (float("nan"), ".nan"),
        (float("inf"), ".inf"),
        (float("-inf"), "-.inf"),
    ],
)
def test_the_shared_token_helper_renders_yamls_spelling(value, token):
    """Python's `nan` / `inf` is a form no note contains; YAML's is what the
    index, the read view and `keyword_search(frontmatter=…)` all agree on."""
    assert vault_service.non_finite_token(value) == token
    assert vault_service.canonical_scalar(value) == token
    assert vault_service.canonical_key(value) == token


@pytest.mark.parametrize("value", [1.5, 0.0, 1, True, False, None, "nan", "x"])
def test_the_token_helper_leaves_everything_else_alone(value):
    """`bool` in particular: `isinstance(True, int)` is true, and a helper
    widened to "numbers" would coerce it."""
    assert vault_service.non_finite_token(value) is None
    assert vault_service.canonical_scalar(value) is value
