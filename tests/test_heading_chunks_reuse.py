"""The context prefix, the reuse key and the certification digest (#355, D2,
D3, D3a), offline.

The PostgreSQL half (append, inserted heading, tag change, the reset race,
convergence) is `tests/integration/test_heading_chunks_pg.py` and
`test_perf_chunk_reuse_pg.py`. Here: the prefix's shape and caps, the reuse
lookup's refusal of NULL prefixes and other chunking versions, the under-lock
recheck comparing the prefix, `certify_embedded`'s stamps, and the grep that
no surface compares chunking versions with `<` or `>`.
"""
import pathlib
import re
from types import SimpleNamespace

import pytest

from src.services import embeddings
from src.services.embeddings import (
    CHUNKING_VERSION,
    EMBED_PREFIX_MAX_CHARS,
    build_embed_prefix,
    embedding_input,
    input_digest,
)
from src.services.index_state import embedding_fingerprint

ROOT = pathlib.Path(__file__).resolve().parent.parent


# ── the prefix ─────────────────────────────────────────────────────────────


def test_prefix_shape_from_the_spec():
    prefix = build_embed_prefix("Alpha", ("Project", "Plan"), ["work"])
    assert embedding_input(prefix, "chunk") == "Alpha > Project > Plan\ntags: work\n\nchunk"


def test_section_zero_is_title_only():
    assert build_embed_prefix("Alpha", (), None) == "Alpha"
    assert build_embed_prefix("Alpha", (), []) == "Alpha"


def test_components_are_whitespace_collapsed_and_cut_at_80():
    prefix = build_embed_prefix("A\t title\n", ("x" * 120,), None)
    title, head = prefix.split(" > ")
    assert title == "A title"
    assert len(head) == 80 and head.endswith("…")


def test_tags_that_do_not_fit_are_omitted_entirely():
    tags = [f"tag{i:03d}" for i in range(60)]
    prefix = build_embed_prefix("T", ("H",), tags)
    assert "tags:" not in prefix and prefix == "T > H"


def test_tags_that_fit_are_kept_in_stored_order():
    assert build_embed_prefix("T", (), ["b", "a"]).endswith("\ntags: b, a")


def test_a_deep_long_breadcrumb_is_cut_from_the_outside():
    path = tuple(f"Heading {i} " + "w" * 70 for i in range(6))
    prefix = build_embed_prefix("Title", path, ["t"])
    assert len(prefix) <= EMBED_PREFIX_MAX_CHARS
    parts = prefix.split("\n")[0].split(" > ")
    assert parts[0] == "Title"
    assert parts[1] == "…"
    assert parts[-1] == embeddings._prefix_component(path[-1])


def test_the_title_and_innermost_always_fit():
    prefix = build_embed_prefix("T" * 500, ("x" * 500,) * 4, None)
    assert len(prefix) <= EMBED_PREFIX_MAX_CHARS
    assert prefix.startswith("T" * 79 + "…") and prefix.endswith("x" * 79 + "…")


def test_input_digest_is_order_sensitive_and_stable():
    assert input_digest([]) == input_digest([])
    assert input_digest(["a", "b"]) != input_digest(["b", "a"])
    assert len(input_digest(["a"])) == 64


# ── the reuse lookup ───────────────────────────────────────────────────────


class _R:
    def __init__(self, rows=(), scalar=None, rowcount=1):
        self._rows = list(rows)
        self._scalar = scalar
        self.rowcount = rowcount

    def all(self):
        return self._rows

    def scalar(self):
        return self._scalar

    def scalar_one_or_none(self):
        return self._scalar


class _LookupSession:
    """Answers the lookup's statements in order: to_regclass, fingerprint,
    the note's chunking version, the rows."""

    def __init__(self, version, rows):
        self.version = version
        self.rows = rows
        self.statements = []

    async def execute(self, stmt, *_a, **_k):
        sql = str(stmt)
        self.statements.append(sql)
        if "to_regclass" in sql:
            return _R(scalar="indexer_state")
        if "indexer_state" in sql:
            return _R(scalar=embedding_fingerprint())
        if "embedded_chunking_version" in sql and "note_embeddings" not in sql:
            return _R(scalar=self.version)
        return _R(rows=self.rows)

    async def commit(self):
        pass

    async def rollback(self):
        pass


async def test_the_lookup_keys_on_prefix_and_text_and_skips_null_prefixes():
    rows = [(1, "chunk", "T > H", [0.1]), (2, "legacy", None, [0.2])]
    found = await embeddings._lookup_reusable_vectors(
        _LookupSession(CHUNKING_VERSION, rows), 5
    )
    assert found == {("T > H", "chunk"): (1, [0.1])}


@pytest.mark.parametrize("version", [None, CHUNKING_VERSION + 1, CHUNKING_VERSION - 1])
async def test_another_chunking_version_never_reuses(version):
    rows = [(1, "chunk", "T > H", [0.1])]
    session = _LookupSession(version, rows)
    assert await embeddings._lookup_reusable_vectors(session, 5) == {}
    assert not any("note_embeddings" in s for s in session.statements)


# ── the under-lock recheck compares the prefix ─────────────────────────────


class _LockSession:
    def __init__(self, surviving):
        self.surviving = surviving

    async def execute(self, stmt, *_a, **_k):
        sql = str(stmt)
        if "to_regclass" in sql:
            return _R(scalar="indexer_state")
        if "pg_advisory_xact_lock" in sql or "lock_timeout" in sql.lower():
            return _R()
        if "indexer_state" in sql:
            return _R(scalar=embedding_fingerprint())
        return _R(rows=self.surviving)


async def test_the_recheck_fails_when_a_reused_rows_prefix_changed():
    note = SimpleNamespace(id=5, file_path="n.md")
    reused = {1: ("chunk", "T > H")}
    assert await embeddings._generation_matches(
        _LockSession([(1, "chunk", "T > H")]), note, reused
    ) is True
    assert await embeddings._generation_matches(
        _LockSession([(1, "chunk", "T > Other")]), note, reused
    ) is False
    assert await embeddings._generation_matches(_LockSession([]), note, reused) is False


# ── certification stamps ───────────────────────────────────────────────────


class _CertSession:
    def __init__(self):
        self.values = None

    async def execute(self, stmt, *_a, **_k):
        self.values = {c.key: v.value for c, v in stmt._values.items()}
        return _R(rowcount=1)

    def expire(self, obj, attrs):
        self.expired = list(attrs)


async def test_certify_stamps_the_version_and_the_digest_and_expires_all_three():
    session = _CertSession()
    note = object()
    await embeddings.certify_embedded(
        session, 1, "h", "n.md", provider_inputs=["a", "b"], expire_on=note
    )
    assert session.values == {
        "embedded_content_hash": "h",
        "embedded_chunking_version": CHUNKING_VERSION,
        "embedded_input_digest": input_digest(["a", "b"]),
    }
    assert set(session.expired) == {
        "embedded_content_hash", "embedded_chunking_version", "embedded_input_digest",
    }


async def test_certify_requires_the_inputs():
    with pytest.raises(TypeError):
        await embeddings.certify_embedded(_CertSession(), 1, "h", "n.md")


async def test_the_uncertified_path_stamps_too():
    class _S:
        def __init__(self):
            self.added = []

        async def execute(self, *_a, **_k):
            return _R(scalar=None)

        async def flush(self):
            pass

        def add(self, row):
            self.added.append(row)

    async def batch(texts, *, kind):
        return [[0.0] for _ in texts]

    note = SimpleNamespace(id=1, file_path="n.md", title="N", tags=None,
                           content_hash="h", embedded_content_hash=None)
    import pytest as _pt

    mp = _pt.MonkeyPatch()
    mp.setattr(embeddings, "get_embeddings_batch", batch)
    try:
        session = _S()
        result = await embeddings.embed_note(session, note, "# A\nbody\n")
    finally:
        mp.undo()
    assert result.outcome is embeddings.NoteEmbedOutcome.EMBEDDED
    assert note.embedded_chunking_version == CHUNKING_VERSION
    assert note.embedded_input_digest == input_digest(["N > A\n\nbody\n"])
    (row,) = session.added
    assert (row.chunk_text, row.heading_path, row.section_ordinal, row.embed_prefix) == (
        "body\n", ["A"], 1, "N > A",
    )


# ── one predicate, never ordered ───────────────────────────────────────────


def test_no_surface_orders_chunking_versions():
    """`IS DISTINCT FROM` everywhere (Codex r2 B2); the only ordered comparison
    allowed is the CHECK's `>= 1` floor in the model and the migration."""
    ordered = re.compile(r"embedded_chunking_version\s*(<=|>=|<|>)\s*(\S+)")
    offenders = []
    for path in sorted((ROOT / "src").rglob("*.py")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for m in ordered.finditer(line):
                if m.group(1) == ">=" and m.group(2).rstrip('"),') == "1":
                    continue  # the CHECK floor
                offenders.append(f"{path.relative_to(ROOT)}:{lineno}: {line.strip()}")
    assert offenders == []
