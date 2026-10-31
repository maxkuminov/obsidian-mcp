"""rerank-documents (#402): the rerank document is the embedding input; every
candidate's primary first, then further fetched chunks in distance order
(≤ 4 per note, ≤ RERANK_MAX_DOCUMENTS); the per-note maximum; the winning
chunk's preview and locator; the hybrid partition (a)/(b)/(c).

Synthetic text only: no tenant note is quoted here.
"""
from __future__ import annotations

import json
import random
from types import SimpleNamespace

import pytest

from tests._rerank_support import fake, fake_server, no_reranker  # noqa: F401
from tests.test_hybrid_rerank import _ExcerptSession
from tests.test_hybrid_search_offline import _offline, kw_row, snip  # noqa: F401
import src.mcp_server.tools as tools
from src.config import settings
from src.services import embeddings, rerank
from src.services.rerank import (
    RERANK_DOC_CHARS, RERANK_MAX_DOCUMENTS_PER_NOTE, Doc, RerankCandidate,
    allocate_documents, candidate_documents, embedding_document,
)


# ── helpers ────────────────────────────────────────────────────────────────


def chunk_row(note_id, path, *, chunk, chunk_index, distance, title=None, prefix=None,
              stamp=2, heading_path=None, ordinal=None, stale=False, tags=None):
    """A projected overfetch row as the `rerank=true` statement returns it."""
    return SimpleNamespace(
        note_id=note_id, chunk_index=chunk_index, chunk_text=chunk, file_path=path,
        title=title if title is not None else path.rsplit("/", 1)[-1][:-3], tags=tags,
        content_hash="h", embedded_content_hash="old" if stale else "h",
        chunks_truncated=False, distance=distance, heading_path=heading_path,
        section_ordinal=ordinal, embed_prefix=prefix, embedded_chunking_version=stamp,
    )


class _Session:
    def __init__(self, rows):
        self.rows = rows
        self.log: list[str] = []

    async def execute(self, clause, *_a, **_k):
        sql = str(clause)
        self.log.append(sql)

        class _R:
            def __init__(self, rows):
                self._rows = rows

            def fetchall(self):
                return self._rows

            def all(self):
                return self._rows

            def scalar(self):
                return 0

        if sql.lstrip().upper().startswith("SET") or "count(" in sql.lower():
            return _R([])
        return _R(self.rows)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return None


@pytest.fixture(autouse=True)
def _quiet_usage(monkeypatch):
    async def _log(*_a, **_k):
        return None

    monkeypatch.setattr(tools, "_log_usage", _log)

    async def _embed(_text, **_kw):
        return [1.0, 0.0, 0.0]

    monkeypatch.setattr(embeddings, "get_embedding", _embed)


def _rows_of(out: str):
    rows = []
    for line in out.splitlines():
        if line.startswith("- {"):
            obj, end = json.JSONDecoder().raw_decode(line[2:])
            rows.append((obj, line[2 + end:]))
    return rows


# ── D1: the builder ────────────────────────────────────────────────────────


def test_embedding_document_is_the_stamped_input_cut_at_2048():
    prefix = "P" * 300
    doc = embedding_document(prefix, "c" * 2000, 2)
    assert len(doc) == RERANK_DOC_CHARS and doc.startswith(prefix + "\n\n")


def test_the_stamp_selects_the_frozen_formatter():
    chunk = "a" + " " * 9 + "b"
    assert embedding_document("T", chunk, 1) == "T\n\n" + chunk  # v1: as stored
    assert embedding_document("T", chunk, 2) == "T\n\na b"  # v2: the >8 run collapsed


@pytest.mark.parametrize("stamp", [None, 3, 0])
def test_an_unknown_stamp_cannot_be_reconstructed(stamp):
    assert embedding_document("T", "body", stamp) is None
    c = candidate_documents("Title", chunks=[_chunk("body", prefix="T > H")], stamp=stamp,
                            fallback_text="EXCERPT")
    assert c.primary.text == "Title" and c.primary.chunk is None and c.secondaries == []


def test_a_null_prefix_row_keeps_the_legacy_document():
    c = candidate_documents("Title", chunks=[_chunk("old chunk", prefix=None)], stamp=None,
                            fallback_text=None)
    assert c.primary.text == "Title\n\nold chunk"


def test_the_code_point_cut_keeps_whole_characters():
    doc = embedding_document("T", "x" * (RERANK_DOC_CHARS - 4) + "\U0001F600" * 3, 2)
    assert len(doc) == RERANK_DOC_CHARS and doc.endswith("\U0001F600")


def test_no_chunks_sends_the_title_and_the_fallback():
    assert candidate_documents("T", chunks=None, stamp=2, fallback_text="ex").primary.text \
        == "T\n\nex"
    assert candidate_documents("T", chunks=[], stamp=2, fallback_text=None).primary.text == "T"


def _chunk(text, *, prefix="T", index=0, distance=0.1, path="n.md", section=None):
    return {"chunk_text": text, "chunk_index": index, "section": section,
            "distance": distance, "file_path": path, "embed_prefix": prefix}


# ── D2: the allocation and its bound (the proof as a property test) ───────


def _random_candidates(rng, m):
    cands = []
    for i in range(m):
        kind = rng.choice(["fresh", "fresh", "fresh", "single"])
        if kind == "single":
            cands.append(RerankCandidate(Doc(f"title {i}")))
            continue
        k = rng.choice([1, 1, 2, 3, 5, 12, 60])
        rows = sorted(
            (_chunk(f"n{i} c{j}", index=j, distance=round(rng.random(), 2), path=f"n{i:02d}.md")
             for j in range(k)),
            key=lambda c: (c["distance"], c["file_path"], c["chunk_index"]),
        )
        cands.append(candidate_documents(f"n{i}", chunks=rows, stamp=2, fallback_text=None))
    return cands


@pytest.mark.parametrize("budget", [50, 60, 100, 200])
@pytest.mark.parametrize("seed", range(25))
def test_the_allocation_bound(budget, seed):
    rng = random.Random(seed * 1000 + budget)
    m = rng.choice([1, 10, 40, 50])
    cands = _random_candidates(rng, m)
    groups = allocate_documents(cands, budget)
    n = sum(len(g) for g in groups)
    # Every candidate holds its primary, first; nothing is unscored.
    assert len(groups) == m
    assert all(g and g[0] is c.primary for g, c in zip(groups, cands))
    # documents ≤ min(budget, 4·m), ≤ 4 per note.
    assert n <= min(budget, RERANK_MAX_DOCUMENTS_PER_NOTE * m)
    assert all(len(g) <= RERANK_MAX_DOCUMENTS_PER_NOTE for g in groups)
    # Every document is one of the candidate's own fetched rows (or its title).
    for g, c in zip(groups, cands):
        own = {id(c.primary), *(id(d) for _k, d in c.secondaries)}
        assert all(id(d) in own for d in g)
    # Secondaries are allocated in global distance order: no unallocated
    # secondary of a note below the cap is nearer than an allocated one.
    taken = {id(d) for g in groups for d in g[1:]}
    allocated_keys = [k for c in cands for k, d in c.secondaries if id(d) in taken]
    if n == budget and allocated_keys:
        worst = max(allocated_keys)
        for g, c in zip(groups, cands):
            if len(g) < RERANK_MAX_DOCUMENTS_PER_NOTE:
                for k, d in c.secondaries:
                    if id(d) not in taken:
                        assert k >= worst
    # Deterministic.
    again = allocate_documents(cands, budget)
    assert [[id(d) for d in g] for g in again] == [[id(d) for d in g] for g in groups]


def test_the_minimum_budget_reproduces_the_incumbent_count():
    cands = [candidate_documents(f"n{i}", chunks=[_chunk("a", path=f"{i}.md", index=0),
                                                  _chunk("b", path=f"{i}.md", index=1)],
                                 stamp=2, fallback_text=None) for i in range(50)]
    groups = allocate_documents(cands, 50)
    assert [len(g) for g in groups] == [1] * 50


def test_secondaries_go_to_the_nearest_chunk_first():
    a = candidate_documents("A", chunks=[_chunk("a1", distance=0.1, path="a.md"),
                                         _chunk("a2", distance=0.40, path="a.md", index=1)],
                            stamp=2, fallback_text=None)
    b = candidate_documents("B", chunks=[_chunk("b1", distance=0.2, path="b.md"),
                                         _chunk("b2", distance=0.30, path="b.md", index=1)],
                            stamp=2, fallback_text=None)
    groups = allocate_documents([a, b], 3)
    assert [d.chunk["chunk_text"] for d in groups[1]] == ["b1", "b2"]
    assert [d.chunk["chunk_text"] for d in groups[0]] == ["a1"]


def test_equidistant_secondaries_tie_on_path_then_chunk_index():
    a = candidate_documents("A", chunks=[_chunk("a1", distance=0.1, path="b.md"),
                                         _chunk("a2", distance=0.5, path="b.md", index=7)],
                            stamp=2, fallback_text=None)
    b = candidate_documents("B", chunks=[_chunk("b1", distance=0.1, path="a.md"),
                                         _chunk("b2", distance=0.5, path="a.md", index=9)],
                            stamp=2, fallback_text=None)
    for _ in range(3):
        groups = allocate_documents([a, b], 3)
        assert len(groups[1]) == 2 and len(groups[0]) == 1


def test_the_per_note_cap():
    rows = [_chunk(f"c{j}", index=j, distance=0.01 * j, path="a.md") for j in range(12)]
    a = candidate_documents("A", chunks=rows, stamp=2, fallback_text=None)
    groups = allocate_documents([a], 100)
    assert [d.chunk["chunk_text"] for d in groups[0]] == ["c0", "c1", "c2", "c3"]


# ── D3/D4: the synthetic #402 fixture, winner allocated by construction ───

_TABLE = {
    # (without the prefix, with the prefix) — the issue's numbers.
    "status olympics argument": (-8.87, -7.0),
    "false division argument": (-3.98, -1.79),
    "concluding argument": (-0.62, 0.33),
    "b prose": (-2.0, -2.0),
    "c prose": (-3.0, -3.0),
}


def _scorer(_q, docs):
    out = []
    for d in docs:
        for key, (plain, prefixed) in _TABLE.items():
            if key in d:
                out.append(prefixed if "Virtue Note >" in d else plain)
                break
        else:
            out.append(-9.0)
    return out


def _fixture_rows():
    a = "Notes/virtue.md"
    return [
        chunk_row(1, a, title="Virtue Note", chunk="status olympics argument", chunk_index=6,
                  distance=0.10, prefix="Virtue Note > Status Olympics", heading_path=["Status Olympics"],
                  ordinal=11),
        chunk_row(2, "Notes/b.md", chunk="b prose", chunk_index=0, distance=0.15,
                  prefix="b", heading_path=[], ordinal=0),
        chunk_row(1, a, title="Virtue Note", chunk="false division argument", chunk_index=7,
                  distance=0.20, prefix="Virtue Note > False Division", heading_path=["False Division"],
                  ordinal=12),
        chunk_row(3, "Notes/c.md", chunk="c prose", chunk_index=0, distance=0.25,
                  prefix="c", heading_path=[], ordinal=0),
        chunk_row(1, a, title="Virtue Note", chunk="concluding argument", chunk_index=10,
                  distance=0.30, prefix="Virtue Note > Conclusion", heading_path=["Conclusion"],
                  ordinal=15),
    ]


async def test_the_issue_ordering_is_repaired_on_semantic_search(monkeypatch, fake):
    rows = _fixture_rows()
    monkeypatch.setattr(tools, "async_session", lambda: _Session(rows))
    fake.scorer = _scorer
    out = await tools.semantic_search_impl("victimhood", limit=3, rerank=True)
    docs = fake.requests[-1]["documents"]
    # The cosine-best chunk, the two further ones and B, C: 5 documents.
    assert len(docs) == 5 and docs[0].startswith("Virtue Note > Status Olympics\n\n")
    page = _rows_of(out)
    assert page[0][0]["path"] == "Notes/virtue.md"
    assert "rerank_score: 0.33" in page[0][1]
    assert page[0][0]["preview"] == "concluding argument"
    assert '§ "Conclusion" (#15)' in page[0][1]
    assert "(#11)" not in page[0][1]
    assert "reranked: true (candidates: 3)" in out.splitlines()[0]


async def test_the_incumbent_builder_loses_the_note(monkeypatch, fake):
    """The pinned regression: one `title + chunk` document per note."""
    from scripts.search_eval_arms.rerank import _incumbent_document

    fake.scorer = _scorer
    docs = [_incumbent_document("Virtue Note", "status olympics argument"),
            _incumbent_document("b", "b prose"), _incumbent_document("c", "c prose")]
    outcome = await rerank.rerank_groups("victimhood", [[d] for d in docs])
    assert outcome.order[-1] == 0


async def test_a_fallback_keeps_the_cosine_best_preview(monkeypatch, fake):
    rows = _fixture_rows()
    monkeypatch.setattr(tools, "async_session", lambda: _Session(rows))
    fake.mode = "status:500"
    out = await tools.semantic_search_impl("victimhood", limit=3, rerank=True)
    page = _rows_of(out)
    assert page[0][0]["preview"] == "status olympics argument"
    assert '(#11)' in page[0][1] and "rerank_score" not in out


async def test_a_winner_without_a_section_shows_no_locator(monkeypatch, fake):
    rows = _fixture_rows()
    rows[4] = chunk_row(1, "Notes/virtue.md", title="Virtue Note", chunk="concluding argument",
                        chunk_index=10, distance=0.30, prefix=None, stamp=2)
    # A NULL-prefix row is sent in the legacy form; make it win outright.
    fake.scorer = lambda q, docs: [5.0 if "concluding" in d else -1.0 for d in docs]
    monkeypatch.setattr(tools, "async_session", lambda: _Session(rows))
    out = await tools.semantic_search_impl("victimhood", limit=3, rerank=True)
    first = _rows_of(out)[0]
    assert first[0]["preview"] == "concluding argument"
    assert "§" not in first[1] and "(#11)" not in first[1]


async def test_an_unknown_stamp_sends_the_title_only(monkeypatch, fake):
    rows = [chunk_row(1, "Notes/new.md", title="New", chunk="SECRET FUTURE TEXT", chunk_index=0,
                      distance=0.1, prefix="New > H", stamp=3, heading_path=["H"], ordinal=1),
            chunk_row(1, "Notes/new.md", title="New", chunk="MORE FUTURE TEXT", chunk_index=1,
                      distance=0.2, prefix="New > H", stamp=3, heading_path=["H"], ordinal=1)]
    monkeypatch.setattr(tools, "async_session", lambda: _Session(rows))
    await tools.semantic_search_impl("q", limit=3, rerank=True)
    body = json.dumps(fake.requests[-1])
    assert fake.requests[-1]["documents"] == ["New"]
    assert "FUTURE" not in body and "New > H" not in body


async def test_a_stale_note_sends_no_stored_text(monkeypatch, fake):
    rows = [chunk_row(1, "Notes/s.md", title="S", chunk="SUPERSEDED", chunk_index=0,
                      distance=0.1, prefix="S > P", stale=True),
            chunk_row(1, "Notes/s.md", title="S", chunk="ALSO OLD", chunk_index=1,
                      distance=0.2, prefix="S > P", stale=True)]
    monkeypatch.setattr(tools, "async_session", lambda: _Session(rows))
    await tools.semantic_search_impl("q", limit=3, rerank=True)
    assert fake.requests[-1]["documents"] == ["S"]


async def test_the_stored_prefix_is_sent_not_the_current_title(monkeypatch, fake):
    rows = [chunk_row(1, "Notes/r.md", title="Renamed", chunk="body", chunk_index=0,
                      distance=0.1, prefix="Old Title > H")]
    monkeypatch.setattr(tools, "async_session", lambda: _Session(rows))
    await tools.semantic_search_impl("q", limit=3, rerank=True)
    assert fake.requests[-1]["documents"] == ["Old Title > H\n\nbody"]


async def test_the_usage_row_records_the_documents(monkeypatch, fake):
    captured = []

    async def _log(tool, params, *_a):
        captured.append(params)

    monkeypatch.setattr(tools, "_log_usage", _log)
    monkeypatch.setattr(tools, "async_session", lambda: _Session(_fixture_rows()))
    await tools.semantic_search_impl("victimhood", limit=3, rerank=True)
    assert captured[-1]["rerank_documents"] == 5 and captured[-1]["rerank_candidates"] == 3
    await tools.semantic_search_impl("victimhood", limit=3)
    assert "rerank_documents" not in captured[-1]


async def test_count_mismatch_is_judged_against_documents(monkeypatch, fake):
    monkeypatch.setattr(tools, "async_session", lambda: _Session(_fixture_rows()))
    fake.mode = "scores:" + json.dumps([1.0, 2.0, 3.0])  # one per note, not per document
    out = await tools.semantic_search_impl("victimhood", limit=3, rerank=True)
    assert "reranked: false (count_mismatch)" in out.splitlines()[0]
    assert _rows_of(out)[0][0]["preview"] == "status olympics argument"


# ── D7: hybrid — the partition, the preview over the snippet ─────────────


def _hsem(i, path, *, chunk, chunk_index=0, distance=0.1, prefix="P", stamp=2,
          heading_path=None, ordinal=None, stale=False, title=None):
    return chunk_row(i, path, chunk=chunk, chunk_index=chunk_index, distance=distance,
                     prefix=prefix, stamp=stamp, heading_path=heading_path, ordinal=ordinal,
                     stale=stale, title=title)


def _serve_h(monkeypatch, session):
    monkeypatch.setattr(tools, "async_session", lambda: session)


async def test_hybrid_partition_and_rollback(monkeypatch, fake):
    keyword = [kw_row(1, "a.md", title="A"), kw_row(2, "kw.md", title="Kw"),
               kw_row(3, "roll.md", title="Roll"), kw_row(4, "stale.md", title="Stale")]
    semantic = [
        _hsem(1, "a.md", chunk="fresh known", prefix="A > H", title="A"),
        _hsem(3, "roll.md", chunk="ROLLBACK TEXT", prefix="Roll > H", stamp=3, title="Roll"),
        _hsem(4, "stale.md", chunk="SUPERSEDED", stale=True, title="Stale"),
    ]
    session = _ExcerptSession(
        excerpts={"kw.md": "kw excerpt", "roll.md": "ROLL EXCERPT", "stale.md": "current"},
        ranked=[keyword], semantic=[semantic], snippet=[[]])
    _serve_h(monkeypatch, session)
    await tools.hybrid_search_impl("q", limit=10, rerank=True)
    docs = fake.requests[-1]["documents"]
    body = json.dumps(fake.requests[-1])
    assert "A > H\n\nfresh known" in docs  # (a)
    assert "Roll" in docs and "ROLLBACK" not in body and "ROLL EXCERPT" not in body  # (b)
    assert "Kw\n\nkw excerpt" in docs and "Stale\n\ncurrent" in docs  # (c)
    assert "SUPERSEDED" not in body


async def test_hybrid_primary_chunk_winner_replaces_the_snippet(monkeypatch, fake):
    keyword = [kw_row(1, "both.md", title="Both"), kw_row(2, "other.md", title="Other")]
    semantic = [_hsem(1, "both.md", chunk="the semantic passage", prefix="Both > Sec",
                      heading_path=["Sec"], ordinal=3, title="Both")]
    answers = dict(excerpts={"other.md": "other excerpt"}, ranked=[keyword],
                   semantic=[semantic], snippet=[[snip(1, hl="a \x02KEYWORD\x03 snippet")]])
    _serve_h(monkeypatch, _ExcerptSession(**{k: (list(v) if isinstance(v, list) else v)
                                             for k, v in answers.items()}))
    fake.scorer = lambda q, docs: [5.0 if "semantic passage" in d else 0.0 for d in docs]
    out = await tools.hybrid_search_impl("q", limit=10, rerank=True)
    first = _rows_of(out)[0]
    assert first[0]["path"] == "both.md"
    assert first[0]["preview_kind"] == "chunk" and first[0]["preview"] == "the semantic passage"
    assert '§ "Sec" (#3)' in first[1]
    # The fallback keeps the incumbent preview selection.
    _serve_h(monkeypatch, _ExcerptSession(**{k: (list(v) if isinstance(v, list) else v)
                                             for k, v in answers.items()}))
    fake.mode = "status:500"
    fb = await tools.hybrid_search_impl("q", limit=10, rerank=True)
    row = next(r for r in _rows_of(fb) if r[0]["path"] == "both.md")
    assert row[0]["preview_kind"] == "snippet"


async def test_hybrid_excerpt_winner_keeps_the_incumbent_preview(monkeypatch, fake):
    keyword = [kw_row(2, "kw.md", title="Kw")]
    session = _ExcerptSession(excerpts={"kw.md": "kw excerpt"}, ranked=[keyword],
                              semantic=[[]], snippet=[[snip(2, hl="a \x02SNIP\x03")]])
    _serve_h(monkeypatch, session)
    out = await tools.hybrid_search_impl("q", limit=10, rerank=True)
    assert _rows_of(out)[0][0]["preview_kind"] == "snippet"


async def test_hybrid_every_fused_candidate_is_scored(monkeypatch, fake):
    keyword = [kw_row(i, f"k{i:02d}.md") for i in range(30)]
    semantic = []
    for i in range(20):
        for j in range(3 if i < 10 else 2):
            semantic.append(_hsem(100 + i, f"s{i:02d}.md", chunk=f"s{i} c{j}", chunk_index=j,
                                  distance=0.01 * i + 0.001 * j))
    semantic = sorted(semantic, key=lambda r: r.distance)[:50]
    session = _ExcerptSession(excerpts={f"k{i:02d}.md": f"kw {i}" for i in range(30)},
                              ranked=[keyword], semantic=[semantic], snippet=[[]])
    _serve_h(monkeypatch, session)
    out = await tools.hybrid_search_impl("q", limit=10, rerank=True)
    assert "reranked: true (candidates: 50)" in out.splitlines()[0]
    assert 50 < len(fake.requests[-1]["documents"]) <= 80


# ── D5 oracle 3: same order and scores, only the winner row's preview and
# locator differ ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("tool", ["semantic", "hybrid"])
async def test_the_oracle_only_the_winner_rows_preview_and_locator_move(monkeypatch, fake, tool):
    def scorer_for(winner_is_secondary):
        def scorer(_q, docs):
            out = []
            for d in docs:
                if "concluding" in d:
                    out.append(0.33 if winner_is_secondary else -5.0)
                elif "status olympics" in d:
                    out.append(-5.0 if winner_is_secondary else 0.33)
                elif "false division" in d:
                    out.append(-6.0)
                elif "b prose" in d:
                    out.append(-2.0)
                else:
                    out.append(-3.0)
            return out
        return scorer

    async def page(winner_is_secondary):
        fake.scorer = scorer_for(winner_is_secondary)
        if tool == "semantic":
            monkeypatch.setattr(tools, "async_session", lambda: _Session(_fixture_rows()))
            return await tools.semantic_search_impl("victimhood", limit=3, rerank=True)
        sem = _fixture_rows()
        _serve_h(monkeypatch, _ExcerptSession(ranked=[[]], semantic=[sem], snippet=[[]]))
        return await tools.hybrid_search_impl("victimhood", limit=3, rerank=True)

    new, ref = await page(True), await page(False)
    a, b = new.splitlines(), ref.splitlines()
    assert len(a) == len(b)
    differing = [i for i, (x, y) in enumerate(zip(a, b)) if x != y]
    assert len(differing) == 1
    (obj_a, tail_a), (obj_b, tail_b) = _rows_of(a[differing[0]])[0], _rows_of(b[differing[0]])[0]
    assert obj_a["path"] == obj_b["path"] == "Notes/virtue.md"
    assert {k: v for k, v in obj_a.items() if k != "preview"} == \
        {k: v for k, v in obj_b.items() if k != "preview"}
    assert obj_a["preview"] == "concluding argument"
    assert obj_b["preview"] == "status olympics argument"

    def strip(t):
        return t.replace('§ "Conclusion" (#15)', "L").replace('§ "Status Olympics" (#11)', "L")

    assert strip(tail_a) == strip(tail_b)


# ── verifier minors (10fa0b6) ──────────────────────────────────────────────


async def test_the_rendered_ordinal_opens_the_section_holding_the_winning_chunk(monkeypatch, fake):
    """Task 4.2: `read_note(section="#N")` with the rendered `#N` returns a
    section whose body contains the winning chunk's `chunk_text`."""
    import re as _re

    from src.services.embeddings import build_embed_prefix, chunk_note_bounded
    from tests.test_heading_chunks import long, section_cleaned

    body = (f"# Status Olympics\n\n{long('alpha')}\n\n"
            f"# False Division\n\n{long('beta')}\n\n"
            f"# Conclusion\n\n{long('gamma')} concluding\n")
    specs, _ = chunk_note_bounded(body, chunk_size=512, overlap=0, max_chunks=64)
    assert len(specs) >= 3
    rows = [
        chunk_row(1, "Notes/virtue.md", title="Virtue Note", chunk=s.text, chunk_index=i,
                  distance=0.1 + 0.01 * i,
                  prefix=build_embed_prefix("Virtue Note", s.heading_path, None),
                  heading_path=list(s.heading_path), ordinal=s.section_ordinal)
        for i, s in enumerate(specs)
    ]
    winner = next(s for s in specs if "concluding" in s.text)
    monkeypatch.setattr(tools, "async_session", lambda: _Session(rows))
    fake.scorer = lambda q, docs: [5.0 if "concluding" in d else -1.0 for d in docs]
    out = await tools.semantic_search_impl("q", limit=3, rerank=True)
    obj, tail = _rows_of(out)[0]
    n = int(_re.search(r"\(#(\d+)\)", tail).group(1))
    assert n == winner.section_ordinal != specs[0].section_ordinal
    assert winner.text in section_cleaned(body, n)


async def test_hybrid_secondary_winner_replaces_the_snippet(monkeypatch, fake):
    """Tasks 4.1/4.4(3): a both-arms note with a NON-empty keyword arm keeps
    its place on its best allocated (secondary) chunk; the preview and the
    locator are that chunk's, not the keyword snippet."""
    keyword = [kw_row(1, "Notes/virtue.md", title="Virtue Note"), kw_row(2, "Notes/b.md"),
               kw_row(3, "Notes/c.md")]
    answers = dict(excerpts={}, ranked=[keyword], semantic=[_fixture_rows()],
                   snippet=[[snip(1, hl="a \x02KEYWORD\x03 snippet")]])
    _serve_h(monkeypatch, _ExcerptSession(**{k: (list(v) if isinstance(v, list) else v)
                                             for k, v in answers.items()}))
    fake.scorer = _scorer
    out = await tools.hybrid_search_impl("victimhood", limit=3, rerank=True)
    first = _rows_of(out)[0]
    assert first[0]["path"] == "Notes/virtue.md"
    assert first[0]["preview_kind"] == "chunk" and first[0]["preview"] == "concluding argument"
    assert '§ "Conclusion" (#15)' in first[1] and "rerank_score: 0.33" in first[1]
    _serve_h(monkeypatch, _ExcerptSession(**{k: (list(v) if isinstance(v, list) else v)
                                             for k, v in answers.items()}))
    fake.mode = "status:500"
    fb = await tools.hybrid_search_impl("victimhood", limit=3, rerank=True)
    row = next(r for r in _rows_of(fb) if r[0]["path"] == "Notes/virtue.md")
    assert row[0]["preview_kind"] == "snippet" and '(#11)' in row[1]
